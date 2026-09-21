"""OCTOCE1 binary envelope and raw uint16 persistence, compatible with MATLAB."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import struct
import zlib
import numpy as np
from .scan import Schedule

MAGIC = b"OCTOCE1\0"
PREFIX = struct.Struct("<8sHHIHHIIQQQII4x")
HEADER_CAPACITY = 65536


def build_header(schedule: Schedule) -> dict:
    p, h = schedule.request, schedule.hardware
    hardware = asdict(h)
    # Established MATLAB metadata keys; these are snapshots, never defaults.
    hardware.update(k_start_nm=h.wavelength_start_nm, k_end_nm=h.wavelength_end_nm,
                    sensor_bit_depth=h.bit_depth, effective_line_rate_hz=h.effective_line_rate_hz,
                    cc1_period_us=h.cc1_period_us)
    return dict(format="OCT/OCE raw acquisition", format_version="1.0",
                software={"name": "octacq", "version": "0.1.0"},
                created_utc=datetime.now(timezone.utc).isoformat(), state="incomplete",
                dtype="<u2", sensor_bits_valid=h.bit_depth,
                scan=asdict(p), hardware=hardware, axis_order=p.axis_order,
                planned_shape=(*p.logical_shape, h.spectral_samples),
                raw_storage={"layout": "C-order, contiguous, no sync or hold samples",
                             "data_offset_bytes": HEADER_CAPACITY,
                             "direction_policy": "BM spatially forward; MB acquisition order, M never reversed"},
                synchronization={"effective_line_rate_hz": h.effective_line_rate_hz,
                                 "cc1_period_us": h.cc1_period_us,
                                 "sweep_period_ticks": schedule.period_ticks,
                                 "camera_delay_s": schedule.camera_delay_s,
                                 "oce_delay_s": schedule.oce_delay_s if p.oce_enabled else None,
                                 "oce_pulse_width_us": h.oce_pulse_width_us if p.oce_enabled else None,
                                 "oce_stride_sweeps": schedule.oce_stride,
                                 "hold_points": schedule.hold_points,
                                 "camera_rearm_us": h.camera_rearm_us,
                                 "bframes_delay_status": "provisional microseconds relative to PFI12; physical validation pending",
                                 "block_boundaries": []},
                integrity={"expected_alines": p.expected_alines, "committed_alines": 0,
                           "lost_camera_buffers": 0, "duplicate_camera_buffers": 0, "reason": None})


@dataclass(frozen=True)
class FileInfo:
    path: Path
    complete: bool
    expected_alines: int
    committed_alines: int
    pixels: int
    data_offset: int
    header: dict


class RawWriter:
    def __init__(self, path: str | Path, header: dict):
        self.path = Path(path)
        self.header = json.loads(json.dumps(header))
        self.expected = int(self.header["integrity"]["expected_alines"])
        self.pixels = int(self.header["planned_shape"][-1])
        self.committed = 0
        self.stream = None
        self._encoded = b""

    def open(self):
        self.stream = self.path.open("x+b", buffering=0)
        try:
            self._header(False)
            self.stream.seek(HEADER_CAPACITY)
        except BaseException:
            self.stream.close()
            self.stream = None
            raise
        return self

    def __enter__(self):
        return self.open()

    def __exit__(self, kind, error, traceback):
        self.close(complete=error is None, reason=str(error) if error else None)

    def _write(self, data):
        view = memoryview(data).cast("B")
        while view:
            count = self.stream.write(view)
            if not count:
                raise OSError("Short binary write")
            view = view[count:]

    def _prefix(self, complete):
        return PREFIX.pack(MAGIC, 1, 0, 2 | int(complete), 1, 0, len(self._encoded),
                           zlib.crc32(self._encoded), HEADER_CAPACITY,
                           self.expected, self.committed, self.pixels, 0)

    def _header(self, complete):
        self.header["integrity"]["committed_alines"] = self.committed
        encoded = json.dumps(self.header, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("utf8")
        if len(encoded) > HEADER_CAPACITY - PREFIX.size:
            raise ValueError("Metadata exceeds reserved header capacity")
        self._encoded = encoded
        self.stream.seek(0)
        self._write(self._prefix(complete) + encoded + bytes(HEADER_CAPACITY - PREFIX.size - len(encoded)))

    def append(self, data):
        if self.stream is None:
            raise RuntimeError("Writer is closed")
        data = np.asarray(data)
        if data.ndim != 2 or data.shape[1] != self.pixels or data.dtype != np.dtype("uint16"):
            raise ValueError("Payload must be uint16 [A-line, pixel]")
        if self.committed + len(data) > self.expected:
            raise ValueError("Payload exceeds request")
        self.stream.seek(HEADER_CAPACITY + self.committed * self.pixels * 2)
        self._write(np.ascontiguousarray(data, dtype="<u2"))
        self.committed += len(data)
        # Prefix always describes fully written rows; JSON is refreshed at close.
        self.stream.seek(0)
        self._write(self._prefix(False))

    def close(self, *, complete: bool, reason: str | None = None):
        if self.stream is None:
            return
        complete = bool(complete and self.committed == self.expected)
        self.header["state"] = "complete" if complete else "incomplete"
        self.header["closed_utc"] = datetime.now(timezone.utc).isoformat()
        self.header["integrity"]["reason"] = reason
        try:
            # Flush payload before advertising a complete acquisition.
            self.stream.flush()
            os.fsync(self.stream.fileno())
            self._header(complete)
            self.stream.flush()
            os.fsync(self.stream.fileno())
        finally:
            self.stream.close()
            self.stream = None


def read_info(path: str | Path) -> FileInfo:
    path = Path(path)
    size = path.stat().st_size
    with path.open("rb") as stream:
        raw = stream.read(PREFIX.size)
        if len(raw) != PREFIX.size:
            raise ValueError("Truncated prefix")
        magic, major, minor, flags, dtype, _, length, crc, offset, expected, committed, pixels, _ = PREFIX.unpack(raw)
        if magic != MAGIC or major != 1 or dtype != 1 or not flags & 2:
            raise ValueError("Unsupported raw format")
        if pixels < 1 or not PREFIX.size <= offset <= size or length > offset - PREFIX.size:
            raise ValueError("Invalid header bounds")
        encoded = stream.read(length)
        if zlib.crc32(encoded) != crc:
            raise ValueError("Metadata CRC mismatch")
        header = json.loads(encoded.decode("utf8"))
    confirmed = min(committed, expected, (size - offset) // (pixels * 2))
    complete = bool(flags & 1) and confirmed == expected
    header["state"] = "complete" if complete else "incomplete"
    header["integrity"]["committed_alines"] = confirmed
    return FileInfo(path, complete, expected, confirmed, pixels, offset, header)


def read_raw(path: str | Path, *, logical: bool = False):
    info = read_info(path)
    if logical and not info.complete:
        raise ValueError("An incomplete file has no complete logical shape")
    shape = (info.committed_alines, info.pixels)
    data = (np.memmap(info.path, mode="r", dtype="<u2", offset=info.data_offset, shape=shape)
            if info.committed_alines else np.empty(shape, dtype="<u2"))
    return data.reshape(info.header["planned_shape"]) if logical else data
