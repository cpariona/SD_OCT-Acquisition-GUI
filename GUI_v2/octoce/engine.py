from __future__ import annotations

import queue
import shutil
import threading
import time
import traceback
from dataclasses import dataclass, field, replace
from enum import Enum
from itertools import count, islice
from pathlib import Path
from typing import Any, Callable

import numpy as np
from numpy.typing import NDArray

from .backends.base import AcquisitionBackend, AcquisitionResult
from .backends.ni_hardware import NIHardwareBackend
from .backends.simulated import SimulatedBackend
from .config import AcquisitionMode, HardwareConfig, ScanParameters, ScanPattern, estimate_payload_bytes
from .processing import preview_complex
from .scan import ScanPlanner, ScanSegment
from .storage import OctBinWriter, build_header


class EngineState(str, Enum):
    IDLE = "idle"
    ARMING = "arming"
    RUNNING = "running"
    STOPPING = "stopping"
    COMPLETED = "completed"
    STOPPED = "stopped"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class EngineEvent:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class _FramePacket:
    segment: ScanSegment
    data: NDArray[np.uint16]
    result: AcquisitionResult


@dataclass(slots=True)
class _Finalize:
    complete: bool
    reason: str | None
    lost_buffers: int
    duplicate_buffers: int


@dataclass(slots=True)
class _PreviewPacket:
    data: NDArray[np.uint16]
    bscan_index: int
    sweep_index: int | None
    remove_dc: bool
    depth_start_bin: int
    depth_end_bin: int
    secondary_data: NDArray[np.uint16] | None = None


class _PreviewWorker:
    """Best-effort preview; its single-slot queue deliberately drops stale views."""

    _STOP = object()

    def __init__(self, event_sink: Callable[[EngineEvent], None], hardware: HardwareConfig):
        self.event_sink = event_sink
        self.hardware = hardware
        self.queue: queue.Queue[_PreviewPacket | object] = queue.Queue(maxsize=1)
        self.thread = threading.Thread(target=self._run, name="octoce-preview", daemon=True)
        self.dropped = 0

    def start(self) -> None:
        self.thread.start()

    def submit(self, packet: _PreviewPacket) -> None:
        try:
            self.queue.put_nowait(packet)
        except queue.Full:
            self.dropped += 1

    def close(self) -> None:
        while self.thread.is_alive():
            try:
                self.queue.put(self._STOP, timeout=0.1)
                break
            except queue.Full:
                continue
        self.thread.join()

    def _run(self) -> None:
        while True:
            packet = self.queue.get()
            try:
                if packet is self._STOP:
                    return
                assert isinstance(packet, _PreviewPacket)
                try:
                    def process(data: NDArray[np.uint16]) -> tuple[NDArray[np.uint16], ...]:
                        col_step = max(1, int(np.ceil(data.shape[0] / 700)))
                        source = np.ascontiguousarray(data[::col_step])
                        intensity, phase, depths, local_alines = preview_complex(
                            source,
                            remove_dc=packet.remove_dc,
                            wavelength_start_nm=self.hardware.k_start_nm,
                            wavelength_end_nm=self.hardware.k_end_nm,
                            depth_start_bin=packet.depth_start_bin,
                            depth_end_bin=packet.depth_end_bin,
                        )
                        return source, intensity, phase, depths, local_alines * col_step

                    source_spectra, intensity_db, phase_rad, depth_indexes, aline_indexes = process(
                        packet.data
                    )
                    secondary: tuple[NDArray[np.uint16], ...] | None = None
                    if packet.secondary_data is not None:
                        secondary = process(packet.secondary_data)
                    self.event_sink(
                        EngineEvent(
                            "preview",
                            {
                                "intensity_db": intensity_db,
                                "phase_rad": phase_rad,
                                "depth_indexes": depth_indexes,
                                "aline_indexes": aline_indexes,
                                "source_spectra": source_spectra,
                                "dc_removed": packet.remove_dc,
                                "depth_start_bin": packet.depth_start_bin,
                                "depth_end_bin": packet.depth_end_bin,
                                "bscan_index": packet.bscan_index,
                                "sweep_index": packet.sweep_index,
                                "calibrated": False,
                                "dropped_previews": self.dropped,
                                "crosshair": secondary is not None,
                                "secondary_source_spectra": secondary[0] if secondary else None,
                                "secondary_intensity_db": secondary[1] if secondary else None,
                                "secondary_phase_rad": secondary[2] if secondary else None,
                                "secondary_depth_indexes": secondary[3] if secondary else None,
                                "secondary_aline_indexes": secondary[4] if secondary else None,
                            },
                        )
                    )
                except Exception as exc:
                    self.event_sink(EngineEvent("preview_warning", {"message": str(exc)}))
            finally:
                self.queue.task_done()


class _WriterWorker:
    def __init__(
        self,
        *,
        scan: ScanParameters,
        hardware: HardwareConfig,
        writer: OctBinWriter | None,
        event_sink: Callable[[EngineEvent], None],
        stop_event: threading.Event,
        preview_remove_dc: Callable[[], bool],
        preview_depth_range: Callable[[], tuple[int, int]],
        preview_enabled: bool,
        continuous: bool,
    ) -> None:
        self.scan = scan
        self.hardware = hardware
        self.writer = writer
        self.event_sink = event_sink
        self.stop_event = stop_event
        self.preview_remove_dc = preview_remove_dc
        self.preview_depth_range = preview_depth_range
        self.preview_enabled = preview_enabled
        self.continuous = continuous
        self.queue: queue.Queue[_FramePacket | _Finalize] = queue.Queue(
            maxsize=hardware.writer_queue_size
        )
        self.thread = threading.Thread(target=self._run, name="octoce-writer", daemon=True)
        self.exception: BaseException | None = None
        self.saved_alines = 0
        self._last_preview = 0.0
        self._mb_bscan: NDArray[np.uint16] | None = None
        self._mb_bscan_index = -1
        self._mb_sweep_index: int | None = None
        self._crosshair_x: NDArray[np.uint16] | None = None
        self._crosshair_key: tuple[int, int | None] | None = None
        self._preview = _PreviewWorker(event_sink, hardware) if preview_enabled else None

    def start(self) -> None:
        if self._preview is not None:
            self._preview.start()
        self.thread.start()

    def put(self, packet: _FramePacket) -> None:
        while True:
            if self.exception is not None:
                raise RuntimeError("Falló el consumidor de almacenamiento/preview.") from self.exception
            try:
                self.queue.put(packet, timeout=0.1)
                return
            except queue.Full:
                if self.stop_event.is_set():
                    raise RuntimeError("Adquisición detenida mientras la cola de escritura estaba llena.")

    def finalize(self, final: _Finalize) -> None:
        if self.exception is not None:
            return
        while self.thread.is_alive():
            try:
                self.queue.put(final, timeout=0.1)
                return
            except queue.Full:
                continue

    def _emit(self, kind: str, **payload: Any) -> None:
        self.event_sink(EngineEvent(kind, payload))

    def _run(self) -> None:
        final = _Finalize(False, "Finalización inesperada del consumidor.", 0, 0)
        try:
            while True:
                item = self.queue.get()
                try:
                    if isinstance(item, _Finalize):
                        final = item
                        break
                    data = item.data[::-1] if item.segment.logical_reverse else item.data
                    if self.writer is not None:
                        self.writer.append(data)
                    self.saved_alines += int(data.shape[0])
                    self._emit(
                        "saved",
                        saved_alines=self.saved_alines,
                        queue_size=self.queue.qsize(),
                        segment_index=item.segment.sequence_index,
                    )
                    if self.preview_enabled:
                        self._maybe_preview(item.segment, data)
                finally:
                    self.queue.task_done()
        except BaseException as exc:
            self.exception = exc
            self.stop_event.set()
            final = _Finalize(False, f"Error de escritura/preview: {exc}", 0, 0)
            self._emit("consumer_error", message=str(exc), traceback=traceback.format_exc())
        finally:
            if self._preview is not None:
                self._preview.close()
            if self.writer is not None:
                try:
                    self.writer.close(
                        complete=final.complete,
                        reason=final.reason,
                        lost_camera_buffers=final.lost_buffers,
                        duplicate_camera_buffers=final.duplicate_buffers,
                    )
                except BaseException as close_exc:
                    if self.exception is None:
                        self.exception = close_exc
                        self.stop_event.set()
                        self._emit("consumer_error", message=str(close_exc), traceback=traceback.format_exc())

    def _maybe_preview(self, segment: ScanSegment, data: NDArray[np.uint16]) -> None:
        now = time.monotonic()
        min_interval = 1.0 / self.hardware.preview_rate_hz
        candidate: NDArray[np.uint16] | None = None
        complete_sweep = self.scan.mode is AcquisitionMode.BM
        if self.scan.mode is AcquisitionMode.BM:
            candidate = data
        elif self.scan.is_stationary:
            candidate = data
            complete_sweep = True
        else:
            if (
                self._mb_bscan is None
                or self._mb_bscan_index != segment.bscan_index
                or self._mb_sweep_index != segment.sweep_index
            ):
                self._mb_bscan = np.zeros(
                    (self.scan.alines, self.hardware.spectral_samples), dtype=np.uint16
                )
                self._mb_bscan_index = segment.bscan_index
                self._mb_sweep_index = segment.sweep_index
            assert segment.aline_index is not None
            averaged = np.rint(data.astype(np.float32).mean(axis=0)).astype(np.uint16)
            linear_reverse = (
                self.scan.pattern is ScanPattern.LINEAR and segment.bscan_index % 2 == 1
            )
            lateral_index = (
                self.scan.alines - 1 - segment.aline_index
                if linear_reverse else segment.aline_index
            )
            self._mb_bscan[lateral_index] = averaged
            last_position = segment.aline_index == self.scan.alines - 1
            complete_sweep = last_position
            if last_position or now - self._last_preview >= min_interval:
                candidate = (
                    self._mb_bscan
                    if linear_reverse else self._mb_bscan[: segment.aline_index + 1]
                )
        if candidate is None:
            return
        try:
            secondary_data: NDArray[np.uint16] | None = None
            if self.scan.pattern is ScanPattern.CROSSHAIR:
                if not complete_sweep:
                    return
                key = (segment.bscan_index, segment.repetition_index)
                if segment.sweep_index == 0:
                    self._crosshair_x = np.array(candidate, dtype=np.uint16, order="C", copy=True)
                    self._crosshair_key = key
                    return
                if segment.sweep_index != 1 or self._crosshair_x is None or self._crosshair_key != key:
                    return
                secondary_data = np.array(candidate, dtype=np.uint16, order="C", copy=True)
                candidate = self._crosshair_x
                self._crosshair_x = None
                self._crosshair_key = None
            if now - self._last_preview < min_interval and (
                self.continuous or self.saved_alines < self.scan.expected_alines
            ):
                return
            self._last_preview = now
            preview_data = (
                np.array(candidate, dtype=np.uint16, order="C", copy=True)
                if self.scan.mode is AcquisitionMode.MB
                else np.ascontiguousarray(candidate)
            )
            assert self._preview is not None
            depth_start_bin, depth_end_bin = self.preview_depth_range()
            self._preview.submit(
                _PreviewPacket(
                    data=preview_data,
                    bscan_index=segment.bscan_index,
                    sweep_index=segment.sweep_index,
                    remove_dc=self.preview_remove_dc(),
                    depth_start_bin=depth_start_bin,
                    depth_end_bin=depth_end_bin,
                    secondary_data=secondary_data,
                )
            )
        except Exception as exc:
            self._emit("preview_warning", message=str(exc))


class AcquisitionEngine:
    def __init__(self, event_sink: Callable[[EngineEvent], None] | None = None):
        self._event_sink = event_sink or (lambda _event: None)
        self._state = EngineState.IDLE
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._preview_remove_dc = True
        self._preview_depth_range = (1, 2048)

    @property
    def state(self) -> EngineState:
        with self._state_lock:
            return self._state

    @property
    def is_active(self) -> bool:
        return self.state in (EngineState.ARMING, EngineState.RUNNING, EngineState.STOPPING)

    @property
    def preview_remove_dc(self) -> bool:
        with self._state_lock:
            return self._preview_remove_dc

    def set_preview_remove_dc(self, enabled: bool) -> None:
        with self._state_lock:
            self._preview_remove_dc = bool(enabled)

    @property
    def preview_depth_range(self) -> tuple[int, int]:
        with self._state_lock:
            return self._preview_depth_range

    def set_preview_depth_range(self, start_bin: int, end_bin: int) -> None:
        if not 1 <= start_bin <= end_bin <= 4096:
            raise ValueError("Rango Z inválido: 1 ≤ inicio ≤ fin ≤ 4096 bins FFT.")
        with self._state_lock:
            self._preview_depth_range = (int(start_bin), int(end_bin))

    def _set_state(self, state: EngineState, **extra: Any) -> None:
        with self._state_lock:
            self._state = state
        self._event_sink(EngineEvent("state", {"state": state.value, **extra}))

    def start(
        self,
        scan: ScanParameters,
        hardware: HardwareConfig,
        *,
        output_path: str | Path | None,
        backend: AcquisitionBackend | None = None,
        continuous: bool = False,
    ) -> None:
        if self.is_active:
            raise RuntimeError("Ya existe una adquisición activa.")
        scan.validate()
        hardware.validate(scan)
        planner = ScanPlanner(scan, hardware)
        output = Path(output_path) if output_path else None
        stationary_mb = scan.mode is AcquisitionMode.MB and scan.is_stationary and scan.bscans == 1
        crosshair_bm = (
            scan.mode is AcquisitionMode.BM
            and scan.pattern is ScanPattern.CROSSHAIR
            and scan.bscans == 1
            and scan.m_repetitions == 1
            and not scan.is_stationary
        )
        if continuous:
            if output is not None:
                raise ValueError("El loop continuo no permite guardar archivo.")
            if not (stationary_mb or crosshair_bm):
                raise ValueError("Loop continuo requiere MB estacionario o BM crosshair.")
            if crosshair_bm and hardware.oce_enabled:
                raise ValueError("BM crosshair continuo es solo preview: deshabilite OCE.")
        if output is not None:
            self._check_disk_space(output, estimate_payload_bytes(scan, hardware))
        selected_backend = backend or SimulatedBackend()
        self._stop_event = threading.Event()
        self._set_state(EngineState.ARMING)
        self._thread = threading.Thread(
            target=self._run,
            args=(scan, hardware, planner, output, selected_backend, continuous),
            name="octoce-acquisition",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self.is_active:
            self._set_state(EngineState.STOPPING)
            self._stop_event.set()

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    @staticmethod
    def _check_disk_space(path: Path, payload_bytes: int) -> None:
        probe = path.parent
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        free = shutil.disk_usage(probe).free
        required = payload_bytes + 64 * 1024
        reserve = max(512 * 1024 * 1024, int(required * 0.05))
        if required + reserve > free:
            raise RuntimeError(
                f"Espacio insuficiente: se requieren aproximadamente "
                f"{(required + reserve) / 2**30:.2f} GiB y hay {free / 2**30:.2f} GiB libres."
            )

    def _run(
        self,
        scan: ScanParameters,
        hardware: HardwareConfig,
        planner: ScanPlanner,
        output: Path | None,
        backend: AcquisitionBackend,
        continuous: bool,
    ) -> None:
        writer: OctBinWriter | None = None
        consumer: _WriterWorker | None = None
        acquired_alines = 0
        acquired_segments = 0
        lost_buffers = 0
        duplicate_buffers = 0
        reason: str | None = None
        complete = False
        acquisition_started = time.monotonic()
        acquisition_elapsed_s = 0.0
        backend_opened = False
        try:
            if output is not None:
                header = build_header(
                    scan,
                    hardware,
                    backend=backend.name,
                    trajectory_sha256=planner.trajectory_digest(),
                )
                writer = OctBinWriter(output, header).open()
            consumer = _WriterWorker(
                scan=scan,
                hardware=hardware,
                writer=writer,
                event_sink=self._event_sink,
                stop_event=self._stop_event,
                preview_remove_dc=lambda: self.preview_remove_dc,
                preview_depth_range=lambda: self.preview_depth_range,
                preview_enabled=output is None,
                continuous=continuous,
            )
            consumer.start()
            backend.open(scan, hardware)
            backend_opened = True
            acquisition_started = time.monotonic()
            self._set_state(EngineState.RUNNING, output=str(output) if output else None)
            previous_copied = -1
            if continuous:
                def iter_continuous_segments():
                    previous_xy = np.asarray((hardware.park_x_mm, hardware.park_y_mm), dtype=np.float64)
                    sequence_index = 0
                    for cycle in count():
                        for base_segment in planner.iter_segments(initial_xy_mm=previous_xy):
                            segment = replace(
                                base_segment,
                                sequence_index=sequence_index,
                                bscan_index=cycle,
                            )
                            yield segment
                            previous_xy = np.array(segment.active_xy_mm[-1], copy=True)
                            sequence_index += 1

                segments = iter_continuous_segments()
            else:
                segments = planner.iter_segments()
            def acquired_items():
                if (isinstance(backend, NIHardwareBackend) and backend.supports_mb_chunks(scan)
                        and backend.mb_chunk_size(scan, hardware) > 0 and not continuous):
                    segment_iter = iter(segments)
                    chunk_size = backend.mb_chunk_size(scan, hardware)
                    while not self._stop_event.is_set():
                        chunk = tuple(islice(segment_iter, chunk_size))
                        if not chunk:
                            break
                        yield from backend.acquire_mb_chunk(chunk, self._stop_event)
                else:
                    for planned in segments:
                        if self._stop_event.is_set():
                            break
                        target = np.empty(
                            (planned.active_count, hardware.spectral_samples), dtype=np.uint16
                        )
                        received = backend.acquire_segment(planned, target, self._stop_event)
                        yield planned, target, received

            for segment, destination, result in acquired_items():
                if self._stop_event.is_set():
                    reason = "Detenida por el usuario."
                    break
                if result.copied_buffer == previous_copied:
                    duplicate_buffers += 1
                    raise RuntimeError(f"Buffer de cámara duplicado: {result.copied_buffer}.")
                previous_copied = result.copied_buffer
                lost_buffers = max(lost_buffers, result.lost_buffers_total)
                packet = _FramePacket(segment, destination, result)
                consumer.put(packet)
                acquired_alines += segment.active_count
                acquired_segments += 1
                elapsed = max(time.monotonic() - acquisition_started, 1e-9)
                fraction = acquired_alines / scan.expected_alines if not continuous else 0.0
                eta = elapsed * (1.0 - fraction) / fraction if fraction > 0 else None
                self._event_sink(
                    EngineEvent(
                        "progress",
                        {
                            "acquired_alines": acquired_alines,
                            "expected_alines": None if continuous else scan.expected_alines,
                            "segment_index": segment.sequence_index,
                            "segments_total": scan.total_segments,
                            "bscan_index": segment.bscan_index,
                            "requested_buffer": result.requested_buffer,
                            "copied_buffer": result.copied_buffer,
                            "ring_index": result.physical_ring_index,
                            "lost_buffers": lost_buffers,
                            "queue_size": consumer.queue.qsize(),
                            "throughput_mib_s": acquired_alines
                            * hardware.spectral_samples
                            * 2
                            / elapsed
                            / 2**20,
                            "eta_s": eta,
                            "elapsed_s": elapsed,
                            "continuous": continuous,
                        },
                    )
                )
            if self._stop_event.is_set() and reason is None:
                reason = "Detenida por el usuario."
            complete = (
                not continuous
                and not self._stop_event.is_set()
                and acquired_alines == scan.expected_alines
                and acquired_segments == scan.total_segments
            )
            if not complete and reason is None and not continuous:
                reason = "La adquisición terminó antes de completar el plan."
        except BaseException as exc:
            if self._stop_event.is_set() and reason is None:
                reason = "Detenida por el usuario."
            else:
                reason = str(exc)
                self._event_sink(EngineEvent("error", {"message": str(exc), "traceback": traceback.format_exc()}))
        finally:
            if backend_opened:
                acquisition_elapsed_s = time.monotonic() - acquisition_started
            if backend_opened:
                try:
                    backend.close(abort=not complete)
                except BaseException as exc:
                    complete = False
                    reason = f"Fallo durante cierre/parqueo: {exc}"
                    self._event_sink(
                        EngineEvent("error", {"message": reason, "traceback": traceback.format_exc()})
                    )
            if consumer is not None:
                consumer.finalize(
                    _Finalize(
                        complete=complete,
                        reason=reason,
                        lost_buffers=lost_buffers,
                        duplicate_buffers=duplicate_buffers,
                    )
                )
                consumer.thread.join()
                if consumer.exception is not None:
                    complete = False
                    reason = f"Fallo del consumidor: {consumer.exception}"
                    if isinstance(backend, NIHardwareBackend):
                        backend.release_warm_camera()
            if complete:
                self._set_state(
                    EngineState.COMPLETED,
                    output=str(output) if output else None,
                    elapsed_s=acquisition_elapsed_s,
                )
            elif self._stop_event.is_set() and reason == "Detenida por el usuario.":
                self._set_state(
                    EngineState.STOPPED, reason=reason,
                    output=str(output) if output else None,
                    elapsed_s=acquisition_elapsed_s,
                )
            else:
                self._set_state(
                    EngineState.ERROR, reason=reason,
                    output=str(output) if output else None,
                    elapsed_s=acquisition_elapsed_s,
                )
