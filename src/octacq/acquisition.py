"""Acquisition lifecycle, bounded raw writer and GUI events."""
from dataclasses import dataclass
from pathlib import Path
import queue
import shutil
import threading
import time
from .config import HardwareProfile, RuntimePolicy, ScanRequest
from .scan import plan
from .storage import RawWriter, build_header, HEADER_CAPACITY
from .hardware.system import HardwareSystem


@dataclass(frozen=True)
class Event:
    kind: str
    state: str
    confirmed_alines: int = 0
    expected_alines: int = 0
    message: str = ""


class Acquisition:
    def __init__(self, profile: HardwareProfile, policy: RuntimePolicy):
        self.profile, self.policy = profile, policy
        self.hardware = HardwareSystem(profile, policy)
        self.events = queue.SimpleQueue()
        self.state = "disconnected"
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()

    @property
    def active(self):
        return self._thread is not None and self._thread.is_alive()

    @property
    def connected(self):
        return self.hardware.connected

    def _emit(self, state, *, message="", confirmed=0, expected=0, kind="state"):
        self.state = state
        self.events.put(Event(kind, state, confirmed, expected, message))

    def _launch(self, state, function, *args):
        with self._lock:
            if self.active:
                raise RuntimeError("An instrument operation is already active")
            self._stop = threading.Event()
            self._emit(state)
            self._thread = threading.Thread(target=function, args=args, name="octacq-acquisition")
            self._thread.start()

    def connect(self, request: ScanRequest):
        self._launch("connecting", self._connection, request)

    def disconnect(self):
        self._launch("disconnecting", self._connection, None)

    def _connection(self, request):
        try:
            if request is None:
                self.hardware.disconnect()
            else:
                plan(request, self.profile)
                self.hardware.connect(request.frame_lines)
            self._emit("idle" if request is not None else "disconnected")
        except Exception as error:
            self._emit("error", message=str(error))

    def start(self, request: ScanRequest, output: str | Path | None, *, continuous=False):
        if not self.connected:
            raise RuntimeError("Connect the instrument first")
        if continuous and output is not None:
            raise ValueError("Continuous alignment does not save a finite file")
        self._launch("arming", self._run, request, Path(output) if output else None, continuous)

    def stop(self):
        with self._lock:
            if self.active and self.state in ("arming", "running", "stopping"):
                self._stop.set()
                self._emit("stopping")

    def join(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)

    def _run(self, request, output, continuous):
        writer = None
        consumer = None
        stream = None
        pending = queue.Queue(maxsize=self.policy.writer_queue_blocks)
        sentinel = object()
        writer_errors = []
        errors = []
        received = 0
        confirmed = [0]
        completed = False

        def write():
            try:
                while True:
                    packet = pending.get()
                    if packet is sentinel:
                        return
                    frame, raw = packet
                    data = raw[::-1] if frame.reverse_storage else raw
                    if writer is not None:
                        writer.append(data)
                    confirmed[0] += len(data)
            except Exception as error:
                writer_errors.append(error)
                self._stop.set()

        try:
            self.hardware.statistics = {}
            schedule = plan(request, self.profile, continuous=continuous)
            # Validate every block before enabling hardware, without retaining them all.
            block_count = 0
            for block in schedule.blocks(self.policy.ao_buffer_bytes):
                block_count += 1
                if self._stop.is_set():
                    raise InterruptedError("Acquisition stopped")
            self.hardware.preflight(schedule)
            if output is not None:
                required = HEADER_CAPACITY + request.expected_alines * self.profile.spectral_samples * 2
                if shutil.disk_usage(output.parent).free < required:
                    raise OSError("Insufficient disk space for the requested raw acquisition")
                header = build_header(schedule)
                header["runtime"] = {
                    "ring_buffers": self.policy.ring_buffers,
                    "writer_queue_blocks": self.policy.writer_queue_blocks,
                    "ao_buffer_bytes": self.policy.ao_buffer_bytes,
                }
                header["synchronization"]["planned_blocks"] = block_count
                header["synchronization"].pop("block_boundaries")
                writer = RawWriter(output, header).open()
            consumer = threading.Thread(target=write, name="octacq-writer")
            consumer.start()
            if self._stop.is_set():
                raise InterruptedError("Acquisition stopped")
            self._emit("running", expected=request.expected_alines)
            last_progress = 0.0
            previous_buffer = None
            stream = self.hardware.execute(schedule, self._stop)
            for frame, number, data in stream:
                if writer_errors:
                    raise writer_errors[0]
                if previous_buffer is not None and number != previous_buffer + 1:
                    raise RuntimeError("Raw buffer sequence is discontinuous")
                previous_buffer = number
                if data.dtype.name != "uint16" or data.shape != (request.frame_lines, self.profile.spectral_samples):
                    raise ValueError("Hardware returned an invalid raw block")
                if self._stop.is_set():
                    raise InterruptedError("Acquisition stopped")
                try:
                    pending.put_nowait((frame, data))
                except queue.Full as error:
                    raise RuntimeError("Raw writer queue full; acquisition cannot preserve every buffer") from error
                received += len(data)
                now = time.monotonic()
                if now - last_progress >= 0.1:
                    self._emit("running", confirmed=confirmed[0], expected=0 if continuous else request.expected_alines,
                               kind="progress")
                    last_progress = now
            completed = not continuous and received == request.expected_alines and not self._stop.is_set()
            if not completed and not self._stop.is_set():
                raise RuntimeError("Hardware ended before the requested acquisition completed")
        except InterruptedError:
            self._stop.set()
        except Exception as error:
            errors.append(error)
        finally:
            if stream is not None:
                try:
                    stream.close()  # forces instrument cleanup even after a writer failure
                except Exception as error:
                    errors.append(error)
            if consumer is not None:
                while consumer.is_alive():
                    try:
                        pending.put(sentinel, timeout=0.05)
                        break
                    except queue.Full:
                        continue
                consumer.join()
            errors.extend(writer_errors)
            completed = completed and not errors and confirmed[0] == request.expected_alines
            reason = "; ".join(str(error) for error in errors) or (None if completed else "Stopped by user")
            if writer is not None:
                writer.header["synchronization"]["execution"] = dict(self.hardware.statistics)
                writer.header["integrity"]["lost_camera_buffers"] = self.hardware.statistics.get("lost_camera_buffers", 0)
                writer.header["integrity"]["duplicate_camera_buffers"] = self.hardware.statistics.get("discontinuous_camera_buffers", 0)
                try:
                    writer.close(complete=completed, reason=reason)
                except Exception as error:
                    errors.append(error)
            if errors:
                self._emit("error", message="; ".join(str(e) for e in errors), confirmed=confirmed[0], expected=request.expected_alines)
            else:
                self._emit("completed" if completed else "stopped", confirmed=confirmed[0], expected=request.expected_alines)
