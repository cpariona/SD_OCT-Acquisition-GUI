from __future__ import annotations

import time
from threading import Event

import numpy as np
from numpy.typing import NDArray

from ..config import AcquisitionMode, HardwareConfig, ScanParameters
from ..scan import ScanSegment
from .base import AcquisitionResult, BackendError, FrameIntegrityError
from .ni_daq import NIDaqGalvoController
from .ni_imaq import NIIMAQCamera


class NIHardwareBackend:
    name = "ni-pcie-6323+1433"

    def __init__(
        self, *, continuous_alignment: bool = False, alignment_block_rate_hz: float | None = None
    ) -> None:
        self._scan: ScanParameters | None = None
        self._config: HardwareConfig | None = None
        self._camera: NIIMAQCamera | None = None
        self._daq: NIDaqGalvoController | None = None
        self._opened = False
        self._lost_total = 0
        self._continuous_alignment = continuous_alignment
        self._alignment_block_rate_hz = alignment_block_rate_hz

    def open(self, scan: ScanParameters, hardware: HardwareConfig) -> None:
        scan.validate()
        hardware.validate(scan)
        if self._continuous_alignment and not (
            scan.mode is AcquisitionMode.MB
            and scan.is_stationary
            and scan.alines == 1
            and scan.bscans == 1
            and scan.sync_points == 0
            and hardware.oce_enabled
        ):
            raise BackendError("El reloj continuo requiere MB estacionario, A=1, B=1, sync=0 y OCE activo.")
        self._scan = scan
        self._config = hardware
        self._camera = NIIMAQCamera()
        self._daq = NIDaqGalvoController(hardware)
        try:
            self._camera.open(
                interface=hardware.camera_interface,
                width=hardware.spectral_samples,
                height=scan.lines_per_segment,
                ring_buffers=hardware.imaq_ring_buffers,
                timeout_ms=hardware.frame_timeout_ms,
                configure_sensor_trigger=hardware.configure_sensor_trigger,
                require_external_sensor_trigger=hardware.require_external_sensor_trigger,
                sensor_trigger_attribute=hardware.sensor_trigger_attribute,
                sensor_trigger_value=hardware.sensor_trigger_value,
                camera_operational_setting=hardware.camera_operational_setting,
                cc1_trigger_width_us=hardware.camera_trigger_width_us,
                cc1_period_us=hardware.cc1_period_us,
                external_buffer_trigger_enabled=hardware.external_buffer_trigger_enabled,
                external_buffer_trigger_line=hardware.external_buffer_trigger_line,
            )
            if self._camera.bits_per_pixel != hardware.sensor_bit_depth:
                raise BackendError(
                    f"NI-IMAQ reporta {self._camera.bits_per_pixel} bits/píxel; "
                    f"la configuración solicita {hardware.sensor_bit_depth}."
                )
            if self._continuous_alignment:
                self._daq.start_continuous_alignment(
                    alines_per_block=scan.m_repetitions,
                    block_rate_hz=self._alignment_block_rate_hz,
                )
            self._opened = True
        except Exception:
            self.close(abort=True)
            raise

    def acquire_segment(
        self,
        segment: ScanSegment,
        destination: NDArray[np.uint16],
        stop_event: Event,
    ) -> AcquisitionResult:
        if not self._opened or self._camera is None or self._daq is None or self._config is None:
            raise BackendError("El backend NI no está abierto.")
        started = time.perf_counter()
        timeout_s = max(
            self._config.frame_timeout_ms / 1000.0,
            segment.ticks / self._config.effective_line_rate_hz + 1.0,
        )
        try:
            if not self._continuous_alignment:
                self._daq.start_segment(segment)
            requested, copied, lost = self._camera.read_into(destination)
            if not self._continuous_alignment:
                self._daq.wait_segment(stop_event, timeout_s)
        except Exception:
            self._daq.abort_segment()
            raise
        if copied != requested:
            raise FrameIntegrityError(f"Buffer solicitado {requested}, copiado {copied}.")
        if lost > self._lost_total:
            delta = lost - self._lost_total
            self._lost_total = lost
            raise FrameIntegrityError(f"NI-IMAQ reportó {delta} buffer(es) perdido(s).")
        return AcquisitionResult(
            requested_buffer=requested,
            copied_buffer=copied,
            physical_ring_index=copied % self._config.imaq_ring_buffers,
            lost_buffers_total=lost,
            elapsed_s=time.perf_counter() - started,
        )

    def close(self, *, abort: bool = False) -> None:
        daq, camera = self._daq, self._camera
        self._opened = False
        if daq is not None:
            try:
                daq.abort_segment()
                daq.park()
            except Exception:
                if not abort:
                    raise
        if camera is not None:
            camera.close(abort=abort)
        self._daq = None
        self._camera = None
