from __future__ import annotations

import json
import os
import struct
import threading
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO

import numpy as np
from numpy.typing import NDArray

from . import __version__
from .config import HardwareConfig, OCE_TRIGGER_DUTY_CYCLE, ScanParameters, ScanPattern


MAGIC = b"OCTOCE1\0"
FORMAT_MAJOR = 1
FORMAT_MINOR = 0
HEADER_CAPACITY = 64 * 1024
FLAG_COMPLETE = 1 << 0
FLAG_LITTLE_ENDIAN = 1 << 1
DTYPE_UINT16 = 1
PREFIX = struct.Struct("<8sHHIHHIIQQQII4x")
assert PREFIX.size == 64


class BinaryFormatError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BinaryFileInfo:
    path: Path
    complete: bool
    committed_alines: int
    expected_alines: int
    pixels_per_aline: int
    data_offset: int
    header: dict[str, Any]

    @property
    def available_shape(self) -> tuple[int, int]:
        return (self.committed_alines, self.pixels_per_aline)


def build_header(
    scan: ScanParameters,
    hardware: HardwareConfig,
    *,
    backend: str,
    trajectory_sha256: str,
) -> dict[str, Any]:
    expected_shape = (*scan.logical_shape_without_pixels, hardware.spectral_samples)
    return {
        "format": "OCT/OCE raw acquisition",
        "format_version": f"{FORMAT_MAJOR}.{FORMAT_MINOR}",
        "software": {"name": "octoce-python", "version": __version__},
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "state": "incomplete",
        "backend": backend,
        "dtype": "<u2",
        "sensor_bits_valid": hardware.sensor_bit_depth,
        "axis_order": list(scan.axis_order),
        "planned_shape": list(expected_shape),
        "raw_storage": {
            "layout": "C-order, contiguous, no sync samples",
            "data_offset_bytes": HEADER_CAPACITY,
            "linear_bidirectional_policy": (
                "BM: reverse-direction sweeps are stored spatially forward; "
                "MB: position groups remain in acquisition order, so odd B-scans "
                "run from positive to negative lateral coordinate. M time samples "
                "within each position are never reversed."
                if scan.pattern is ScanPattern.LINEAR else None
            ),
            "sync_policy": (
                "AO transition and post-OCE hold points are not camera-triggered or stored"
            ),
        },
        "scan": scan.to_dict(),
        "hardware": hardware.to_dict(),
        "synchronization": {
            "timing_owner": "NI-DAQmx hardware tasks",
            "ao_channels": [f"{hardware.daq_device}/ao0", f"{hardware.daq_device}/ao1"],
            "camera_trigger": hardware.camera_trigger_terminal,
            "camera_trigger_policy": (
                "one PFI12 pulse per acquisition segment; PCIe-1433 pattern generator "
                "produces one CC1 pulse per valid A-line"
            ),
            "camera_trigger_width_us": hardware.camera_trigger_width_us,
            "camera_phase_offset_us": hardware.camera_phase_offset_us,
            "cc1_period_us": hardware.cc1_period_us,
            "effective_line_rate_hz": hardware.effective_line_rate_hz,
            "oce_trigger": hardware.oce_trigger_terminal,
            "oce_enabled": hardware.oce_enabled,
            "oce_policy": (
                "MB: one pulse per group of M A-lines; BM: one pulse per repeated B-scan "
                "(M pulses total per spatial B-scan). Crosshair X+Y counts as one B-scan."
            ),
            "bframes_delay_us": scan.bframes_delay_us,
            "oce_pulse_width_us": hardware.oce_pulse_width_us,
            "oce_duty_cycle": OCE_TRIGGER_DUTY_CYCLE,
            "oce_pulse_period_us": hardware.oce_pulse_width_us / OCE_TRIGGER_DUTY_CYCLE,
            "start_order": ["NI-IMAQ", "camera counter", "OCE counter", "AO master"],
        },
        "calibration": {
            "x_v_per_mm": hardware.x_v_per_mm,
            "y_v_per_mm": hardware.y_v_per_mm,
            "k_linearization": None,
            "dispersion_compensation": None,
        },
        "trajectory_sha256": trajectory_sha256,
        "hardware_safety_warnings": list(hardware.safety_warnings(scan)),
        "integrity": {
            "expected_alines": scan.expected_alines,
            "committed_alines": 0,
            "lost_camera_buffers": 0,
            "duplicate_camera_buffers": 0,
            "reason": None,
        },
        "notes": [
            "The displayed OCT preview is diagnostic until k-linearization and dispersion calibration are supplied."
        ],
    }


class OctBinWriter:
    """Crash-tolerant streaming writer for raw uint16 A-lines.

    The 64-byte prefix is refreshed after every committed block.  JSON occupies
    a reserved area, so final counters can be updated without moving the payload.
    """

    def __init__(self, path: str | os.PathLike[str], header: dict[str, Any]):
        self.path = Path(path)
        self.header = json.loads(json.dumps(header))
        self.expected_alines = int(self.header["integrity"]["expected_alines"])
        self.pixels = int(self.header["planned_shape"][-1])
        self.committed_alines = 0
        self._closed = False
        self._lock = threading.Lock()
        self._fh: BinaryIO | None = None
        self._header_json = b""
        self._last_prefix_commit_alines = 0
        self._last_prefix_commit_time = time.monotonic()

    def open(self) -> "OctBinWriter":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("x+b", buffering=0)
        try:
            self._write_header(complete=False)
            self._fh.seek(HEADER_CAPACITY)
        except BaseException:
            self._fh.close()
            self._fh = None
            self._closed = True
            raise
        return self

    def __enter__(self) -> "OctBinWriter":
        return self.open()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close(
            complete=exc is None and self.committed_alines == self.expected_alines,
            reason=None if exc is None else str(exc),
        )

    def append(self, spectra: NDArray[np.uint16]) -> None:
        if self._fh is None or self._closed:
            raise BinaryFormatError("El archivo no está abierto.")
        array = np.asarray(spectra)
        if array.ndim != 2 or array.shape[1] != self.pixels:
            raise BinaryFormatError(
                f"Bloque inválido {array.shape}; se esperaba [N, {self.pixels}]."
            )
        if self.committed_alines + array.shape[0] > self.expected_alines:
            raise BinaryFormatError("El bloque excede el número de A-lines planificado.")
        packed = np.ascontiguousarray(array, dtype="<u2")
        with self._lock:
            self._fh.seek(0, os.SEEK_END)
            self._fh.write(memoryview(packed).cast("B"))
            self.committed_alines += int(packed.shape[0])
            now = time.monotonic()
            if (
                self.committed_alines == self.expected_alines
                or self.committed_alines - self._last_prefix_commit_alines >= 4096
                or now - self._last_prefix_commit_time >= 0.25
            ):
                self._write_prefix(complete=False)
                self._last_prefix_commit_alines = self.committed_alines
                self._last_prefix_commit_time = now

    def close(
        self,
        *,
        complete: bool,
        reason: str | None = None,
        lost_camera_buffers: int = 0,
        duplicate_camera_buffers: int = 0,
    ) -> None:
        if self._closed:
            return
        if self._fh is None:
            self._closed = True
            return
        complete = bool(complete and self.committed_alines == self.expected_alines)
        self.header["state"] = "complete" if complete else "incomplete"
        self.header["closed_utc"] = datetime.now(timezone.utc).isoformat()
        integrity = self.header["integrity"]
        integrity["committed_alines"] = self.committed_alines
        integrity["lost_camera_buffers"] = int(lost_camera_buffers)
        integrity["duplicate_camera_buffers"] = int(duplicate_camera_buffers)
        integrity["reason"] = reason
        with self._lock:
            try:
                self._write_header(complete=complete)
                self._fh.flush()
                os.fsync(self._fh.fileno())
            finally:
                try:
                    self._fh.close()
                finally:
                    self._fh = None
                    self._closed = True

    def _json_bytes(self) -> bytes:
        encoded = json.dumps(
            self.header,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        available = HEADER_CAPACITY - PREFIX.size
        if len(encoded) > available:
            raise BinaryFormatError(
                f"El header JSON ({len(encoded)} bytes) excede la reserva ({available} bytes)."
            )
        return encoded

    def _prefix_bytes(self, json_bytes: bytes, *, complete: bool) -> bytes:
        flags = FLAG_LITTLE_ENDIAN | (FLAG_COMPLETE if complete else 0)
        return PREFIX.pack(
            MAGIC,
            FORMAT_MAJOR,
            FORMAT_MINOR,
            flags,
            DTYPE_UINT16,
            0,
            len(json_bytes),
            zlib.crc32(json_bytes) & 0xFFFFFFFF,
            HEADER_CAPACITY,
            self.expected_alines,
            self.committed_alines,
            self.pixels,
            0,
        )

    def _write_prefix(self, *, complete: bool) -> None:
        assert self._fh is not None
        json_bytes = self._header_json or self._json_bytes()
        position = self._fh.tell()
        self._fh.seek(0)
        self._fh.write(self._prefix_bytes(json_bytes, complete=complete))
        self._fh.seek(position)

    def _write_header(self, *, complete: bool) -> None:
        assert self._fh is not None
        self.header["integrity"]["committed_alines"] = self.committed_alines
        json_bytes = self._json_bytes()
        self._header_json = json_bytes
        padding = HEADER_CAPACITY - PREFIX.size - len(json_bytes)
        self._fh.seek(0)
        self._fh.write(self._prefix_bytes(json_bytes, complete=complete))
        self._fh.write(json_bytes)
        self._fh.write(b"\0" * padding)


def read_info(path: str | os.PathLike[str]) -> BinaryFileInfo:
    source = Path(path)
    with source.open("rb") as fh:
        prefix = fh.read(PREFIX.size)
        if len(prefix) != PREFIX.size:
            raise BinaryFormatError("Archivo demasiado corto para contener un header OCT/OCE.")
        (
            magic,
            major,
            _minor,
            flags,
            dtype_code,
            _reserved,
            json_len,
            json_crc,
            data_offset,
            expected,
            committed,
            pixels,
            _reserved2,
        ) = PREFIX.unpack(prefix)
        if magic != MAGIC:
            raise BinaryFormatError("Magic inválido; no es un archivo OCT/OCE compatible.")
        if major != FORMAT_MAJOR:
            raise BinaryFormatError(f"Versión mayor no soportada: {major}.")
        if dtype_code != DTYPE_UINT16:
            raise BinaryFormatError(f"dtype no soportado: código {dtype_code}.")
        if pixels < 1:
            raise BinaryFormatError("El número de píxeles por A-line es inválido.")
        if data_offset < PREFIX.size or data_offset > source.stat().st_size:
            raise BinaryFormatError("El offset del payload es inválido.")
        if json_len > data_offset - PREFIX.size:
            raise BinaryFormatError("Longitud JSON inválida.")
        encoded = fh.read(json_len)
        if zlib.crc32(encoded) & 0xFFFFFFFF != json_crc:
            raise BinaryFormatError("CRC del header JSON inválido.")
        header = json.loads(encoded.decode("utf-8"))
    max_by_size = max(0, (source.stat().st_size - data_offset) // (pixels * 2))
    safe_committed = min(int(committed), int(max_by_size), int(expected))
    return BinaryFileInfo(
        path=source,
        complete=bool(flags & FLAG_COMPLETE) and safe_committed == expected,
        committed_alines=safe_committed,
        expected_alines=int(expected),
        pixels_per_aline=int(pixels),
        data_offset=int(data_offset),
        header=header,
    )


def open_memmap(path: str | os.PathLike[str], *, logical_shape: bool = False) -> np.memmap:
    info = read_info(path)
    data = np.memmap(
        info.path,
        dtype="<u2",
        mode="r",
        offset=info.data_offset,
        shape=info.available_shape,
        order="C",
    )
    if logical_shape:
        planned = tuple(int(v) for v in info.header["planned_shape"])
        if not info.complete:
            raise BinaryFormatError("Un archivo incompleto no puede adoptar la forma lógica completa.")
        return data.reshape(planned)
    return data


def wait_for_stable_size(path: Path, interval_s: float = 0.02) -> int:
    """Small utility used by tests and external readers following a live file."""
    first = path.stat().st_size
    time.sleep(interval_s)
    second = path.stat().st_size
    return second if first == second else -1
