"""Camera/DAQ session coordination for this laboratory instrument."""
import time
from math import ceil
from ..config import HardwareProfile, RuntimePolicy
from ..scan import Schedule
from .camera import Camera
from .daq import Daq


class HardwareSystem:
    def __init__(self, profile: HardwareProfile, policy: RuntimePolicy):
        self.profile, self.policy = profile, policy
        self.camera = Camera(profile, policy)
        self.daq = Daq(profile, policy)
        self.connected = False
        self.active = False
        self.statistics = {}

    def connect(self, frame_lines: int):
        if self.active:
            raise RuntimeError("Cannot reconfigure an active instrument")
        self.connected = False
        self.camera.connect(frame_lines)
        self.connected = True

    def execute(self, schedule: Schedule, stop_event):
        if self.active or not self.connected:
            raise RuntimeError("Instrument must be connected and idle")
        if schedule.hardware != self.profile:
            raise ValueError("Schedule belongs to a different hardware profile")
        if self.profile.camera_rearm_us is None:
            raise ValueError("Measure and set camera.camera_rearm_us in system.toml before physical acquisition")
        self.connect(schedule.request.frame_lines)
        self.camera.set_read_timeout(self.policy.frame_timeout_ms + ceil(schedule.period_s * 1000))
        self.active = True
        self.statistics = dict(block_count=0, block_setup_max_s=0.0, block_setup_total_s=0.0,
                               first_buffer=None, last_buffer=None,
                               lost_camera_buffers=0, discontinuous_camera_buffers=0)
        failed = True
        previous_end = None
        try:
            for block in schedule.blocks(self.policy.ao_buffer_bytes):
                if stop_event.is_set():
                    raise InterruptedError("Acquisition stopped")
                if schedule.continuous:
                    self.daq.move_to(block.volts[-1])
                self.daq.prepare(schedule, block)
                if stop_event.is_set():
                    raise InterruptedError("Acquisition stopped")
                self.daq.start()
                started = time.monotonic()
                self.statistics["block_count"] += 1
                if previous_end is not None:
                    gap = started - previous_end
                    self.statistics["block_setup_max_s"] = max(self.statistics["block_setup_max_s"], gap)
                    self.statistics["block_setup_total_s"] += gap
                while True:
                    for frame in block.frames:
                        if stop_event.is_set():
                            raise InterruptedError("Acquisition stopped")
                        number, raw = self.camera.read()
                        if self.statistics["first_buffer"] is None:
                            self.statistics["first_buffer"] = number
                        self.statistics["last_buffer"] = number
                        yield frame, number, raw
                    if not schedule.continuous:
                        break
                self.daq.wait(stop_event, schedule.period_s * len(block.frames))
                self.daq.stop()
                previous_end = time.monotonic()
            failed = False
        finally:
            errors = []
            for action in (self.daq.stop, self.daq.park):
                try:
                    action()
                except Exception as error:
                    errors.append(error)
            self.statistics["lost_camera_buffers"] = self.camera.lost_buffers
            self.statistics["discontinuous_camera_buffers"] = self.camera.discontinuous_buffers
            if failed or errors or stop_event.is_set():
                # A partial frame/aborted exposure makes the pending ring untrustworthy.
                try:
                    self.camera.close()
                except Exception as error:
                    errors.append(error)
                self.connected = False
            self.active = False
            if errors:
                raise ExceptionGroup("Instrument cleanup failed", errors)

    def disconnect(self):
        if self.active:
            raise RuntimeError("Stop acquisition before disconnecting")
        errors = []
        for action in (self.daq.stop, self.daq.park, self.camera.close):
            try:
                action()
            except Exception as error:
                errors.append(error)
        self.connected = False
        if errors:
            raise ExceptionGroup("Instrument disconnect failed", errors)
