from __future__ import annotations

import atexit
import time
from threading import Event, Lock, Timer
from typing import Iterator, Sequence

import numpy as np
from numpy.typing import NDArray

from ..config import AcquisitionMode, HardwareConfig, ScanParameters, ScanPattern, optimized_scan_period_ticks
from ..scan import ScanSegment
from .base import AcquisitionResult, BackendError, FrameIntegrityError
from .ni_daq import NIDaqGalvoController
from .ni_imaq import NIIMAQCamera


class NIHardwareBackend:
    name = "ni-pcie-6323+1433-optimized"
    _warm_lock = Lock()
    _warm_camera: tuple[tuple[object, ...], NIIMAQCamera] | None = None
    _warm_timer: Timer | None = None
    _warm_token: object | None = None
    WARM_IDLE_SECONDS = 30.0

    def __init__(
        self, *, continuous_alignment: bool = False, alignment_block_rate_hz: float | None = None,
        warm_camera: bool = False,
    ) -> None:
        self._scan: ScanParameters | None = None
        self._config: HardwareConfig | None = None
        self._camera: NIIMAQCamera | None = None
        self._daq: NIDaqGalvoController | None = None
        self._opened = False
        self._lost_total = 0
        self._continuous_alignment = continuous_alignment
        self._alignment_block_rate_hz = alignment_block_rate_hz
        self._warm_enabled = warm_camera and not continuous_alignment
        self._camera_signature: tuple[object, ...] | None = None

    @classmethod
    def release_warm_camera(cls, token: object | None = None) -> None:
        """Release the idle NI-IMAQ session (also invoked by the idle timer)."""
        with cls._warm_lock:
            if token is not None and token is not cls._warm_token:
                return
            cached = cls._warm_camera
            timer = cls._warm_timer
            cls._warm_camera = None
            cls._warm_timer = None
            cls._warm_token = None
            if timer is not None:
                timer.cancel()
            if cached is not None:
                cached[1].close(abort=True)

    @classmethod
    def _take_warm_camera(cls, signature: tuple[object, ...]) -> NIIMAQCamera | None:
        with cls._warm_lock:
            cached = cls._warm_camera
            timer = cls._warm_timer
            cls._warm_camera = None
            cls._warm_timer = None
            cls._warm_token = None
            if timer is not None:
                timer.cancel()
            if cached is None:
                return None
            if cached[0] == signature:
                return cached[1]
            cached[1].close(abort=True)
            return None

    @classmethod
    def _store_warm_camera(cls, signature: tuple[object, ...], camera: NIIMAQCamera) -> None:
        cls.release_warm_camera()
        token = object()
        timer = Timer(cls.WARM_IDLE_SECONDS, cls.release_warm_camera, args=(token,))
        timer.daemon = True
        with cls._warm_lock:
            cls._warm_camera = (signature, camera)
            cls._warm_timer = timer
            cls._warm_token = token
        timer.start()

    @staticmethod
    def mb_chunk_size(scan: ScanParameters, hardware: HardwareConfig) -> int:
        """Bound AO buffers and preserve complete X/Y pairs in BM crosshair."""
        nominal_ticks = scan.sync_points + scan.lines_per_segment + hardware.oce_post_hold_points(scan)
        stride = 2 if scan.mode is AcquisitionMode.BM and scan.pattern is ScanPattern.CROSSHAIR else 1
        ticks = optimized_scan_period_ticks(
            hardware, active_start=scan.sync_points, active_count=scan.lines_per_segment,
            nominal_ticks=nominal_ticks, bframes_delay_us=scan.bframes_delay_us,
            oce_stride=stride,
        )
        size = max(1, min(64, 1_000_000 // max(1, ticks)))
        if scan.mode is AcquisitionMode.BM and scan.pattern is ScanPattern.CROSSHAIR:
            size -= size % 2
        return size

    def supports_mb_chunks(self, scan: ScanParameters) -> bool:
        # Moving trajectories need a sync transition. Stationary MB can use
        # an unacquired hold even with sync=0 because no galvo movement occurs.
        return (
            not self._continuous_alignment
            and (scan.sync_points > 0 or (scan.mode is AcquisitionMode.MB and scan.is_stationary))
            and scan.total_segments > 1
            and (scan.mode is AcquisitionMode.MB or scan.pattern is not ScanPattern.CROSSHAIR
                 or scan.total_segments >= 2)
        )

    def acquire_mb_chunk(
        self, segments: Sequence[ScanSegment], stop_event: Event
    ) -> Iterator[tuple[ScanSegment, NDArray[np.uint16], AcquisitionResult]]:
        if not self._opened or self._camera is None or self._daq is None or self._config is None:
            raise BackendError("El backend NI no está abierto.")
        if not segments:
            return
        ticks = self._daq.start_mb_chunk(segments)
        timeout_s = max(
            self._config.frame_timeout_ms / 1000.0,
            ticks * len(segments) / self._config.effective_line_rate_hz + 1.0,
        )
        try:
            for segment in segments:
                if stop_event.is_set():
                    raise BackendError("Adquisición detenida por el usuario.")
                destination = np.empty(
                    (segment.active_count, self._config.spectral_samples), dtype=np.uint16
                )
                started = time.perf_counter()
                requested, copied, lost = self._camera.read_into(destination)
                if copied != requested:
                    raise FrameIntegrityError(f"Buffer solicitado {requested}, copiado {copied}.")
                if lost > self._lost_total:
                    delta = lost - self._lost_total
                    self._lost_total = lost
                    raise FrameIntegrityError(f"NI-IMAQ reportó {delta} buffer(es) perdido(s).")
                yield (
                    segment,
                    destination,
                    AcquisitionResult(
                        requested_buffer=requested,
                        copied_buffer=copied,
                        physical_ring_index=copied % self._config.imaq_ring_buffers,
                        lost_buffers_total=lost,
                        elapsed_s=time.perf_counter() - started,
                    ),
                )
            self._daq.wait_segment(stop_event, timeout_s)
        finally:
            self._daq.abort_segment()

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
        camera_kwargs = dict(
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
        self._camera_signature = tuple(camera_kwargs.values())
        if self._warm_enabled:
            self._camera = self._take_warm_camera(self._camera_signature)
        else:
            self.release_warm_camera()
            self._camera = None
        needs_camera_open = self._camera is None
        if self._camera is None:
            self._camera = NIIMAQCamera()
        self._daq = NIDaqGalvoController(hardware)
        try:
            if needs_camera_open:
                self._camera.open(**camera_kwargs)
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
        park_error: Exception | None = None
        if daq is not None:
            try:
                daq.abort_segment()
                daq.park()
            except Exception as exc:
                park_error = exc
        if camera is not None:
            if self._warm_enabled and not abort and park_error is None and self._camera_signature is not None:
                self._store_warm_camera(self._camera_signature, camera)
            else:
                camera.close(abort=abort or park_error is not None)
        self._daq = None
        self._camera = None
        if park_error is not None and not abort:
            raise park_error


atexit.register(NIHardwareBackend.release_warm_camera)
