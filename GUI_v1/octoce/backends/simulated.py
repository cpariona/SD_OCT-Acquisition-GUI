from __future__ import annotations

import time
from threading import Event

import numpy as np
from numpy.typing import NDArray

from ..config import HardwareConfig, ScanParameters
from ..scan import ScanSegment
from .base import AcquisitionResult, BackendError


class SimulatedBackend:
    name = "simulation"

    def __init__(self, *, realtime_factor: float = 8.0, seed: int = 20260911):
        self.realtime_factor = max(float(realtime_factor), 0.1)
        self._rng = np.random.default_rng(seed)
        self._scan: ScanParameters | None = None
        self._hardware: HardwareConfig | None = None
        self._buffer_number = 0
        self._opened = False

    def open(self, scan: ScanParameters, hardware: HardwareConfig) -> None:
        scan.validate()
        hardware.validate(scan)
        self._scan = scan
        self._hardware = hardware
        self._buffer_number = 0
        self._opened = True

    def acquire_segment(
        self,
        segment: ScanSegment,
        destination: NDArray[np.uint16],
        stop_event: Event,
    ) -> AcquisitionResult:
        if not self._opened or self._hardware is None or self._scan is None:
            raise BackendError("El simulador no está abierto.")
        if destination.shape != (segment.active_count, self._hardware.spectral_samples):
            raise BackendError(
                f"Buffer destino {destination.shape}; esperado "
                f"({segment.active_count}, {self._hardware.spectral_samples})."
            )
        started = time.perf_counter()
        duration = segment.ticks / self._hardware.effective_line_rate_hz / self.realtime_factor
        if stop_event.wait(duration):
            raise BackendError("Adquisición simulada detenida por el usuario.")
        self._fill_spectra(segment.active_xy_mm, destination)
        number = self._buffer_number
        self._buffer_number += 1
        return AcquisitionResult(
            requested_buffer=number,
            copied_buffer=number,
            physical_ring_index=number % self._hardware.imaq_ring_buffers,
            lost_buffers_total=0,
            elapsed_s=time.perf_counter() - started,
        )

    def _fill_spectra(self, xy_mm: NDArray[np.float64], out: NDArray[np.uint16]) -> None:
        assert self._hardware is not None
        rows, pixels = out.shape
        spectral_axis = np.arange(pixels, dtype=np.float32) / pixels
        chunk_size = 128
        sensor_max = float((1 << self._hardware.sensor_bit_depth) - 1)
        for first in range(0, rows, chunk_size):
            last = min(first + chunk_size, rows)
            x = xy_mm[first:last, 0].astype(np.float32)[:, None]
            y = xy_mm[first:last, 1].astype(np.float32)[:, None]
            surface_bin = 105.0 + 12.0 * np.sin(0.8 * x) + 8.0 * np.cos(0.7 * y)
            deeper_bin = 230.0 + 18.0 * np.sin(0.45 * x + 0.3 * y)
            phase = 0.18 * x + 0.11 * y
            k = spectral_axis[None, :]
            signal = (
                2048.0
                + 720.0 * np.cos(2.0 * np.pi * surface_bin * k + phase)
                + 320.0 * np.cos(2.0 * np.pi * deeper_bin * k - 0.4 * phase)
                + 90.0 * np.cos(2.0 * np.pi * 18.0 * k)
            )
            noise = self._rng.normal(0.0, 18.0, size=signal.shape)
            out[first:last] = np.clip(signal + noise, 0.0, sensor_max).astype(np.uint16)

    def close(self, *, abort: bool = False) -> None:
        self._opened = False
