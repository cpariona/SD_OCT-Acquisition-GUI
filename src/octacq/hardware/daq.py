"""Direct NI-DAQmx AO master and camera/OCE counter trains."""
import time
import numpy as np
from ..config import HardwareProfile, RuntimePolicy
from ..scan import Block, Schedule, transition


class Daq:
    def __init__(self, profile: HardwareProfile, policy: RuntimePolicy):
        self.profile, self.policy = profile, policy
        self.tasks = []
        self.ao = None
        self.waveform = None
        self.started = False
        self.continuous = False
        self.position = np.asarray(profile.park_volts, dtype=float)
        self.energized = False
        self._started_tasks = []

    def prepare(self, schedule: Schedule, block: Block):
        import nidaqmx
        from nidaqmx.constants import AcquisitionType, Edge, Level, TaskMode
        from nidaqmx.stream_writers import AnalogMultiChannelWriter
        if self.tasks:
            raise RuntimeError("DAQ tasks are already prepared")
        if self.position is None:
            raise RuntimeError("AO position is unknown; restore park physically and restart the instrument session")
        h, p = self.profile, schedule.request
        self.waveform = block.volts
        self.continuous = schedule.continuous
        try:
            ao = nidaqmx.Task()
            self.tasks.append(ao)
            self.ao = ao
            for channel in (h.ao_x, h.ao_y):
                ao.ao_channels.add_ao_voltage_chan(f"{h.daq_device}/{channel}", min_val=h.ao_min_v, max_val=h.ao_max_v)
            mode = AcquisitionType.CONTINUOUS if schedule.continuous else AcquisitionType.FINITE
            ao.timing.cfg_samp_clk_timing(rate=h.effective_line_rate_hz, sample_mode=mode,
                                         samps_per_chan=len(block.volts))
            count = AnalogMultiChannelWriter(ao.out_stream, auto_start=False).write_many_sample(
                np.ascontiguousarray(block.volts.T), timeout=self.policy.frame_timeout_ms / 1000)
            if count != len(block.volts):
                raise RuntimeError("Short AO preload")
            events = [(h.camera_counter, h.camera_trigger_terminal, schedule.camera_delay_s,
                       h.cc1_trigger_width_us * 1e-6, schedule.period_s, len(block.frames))]
            if p.oce_enabled:
                events.append((h.oce_counter, h.oce_trigger_terminal, schedule.oce_delay_s,
                               h.oce_pulse_width_us * 1e-6, schedule.period_s * schedule.oce_stride,
                               sum(frame.oce for frame in block.frames)))
            channels = []
            for counter, terminal, delay, width, period, pulses in events:
                if period <= width or pulses < 1:
                    raise ValueError("Counter pulse cannot fit its period")
                task = nidaqmx.Task()
                self.tasks.append(task)
                channel = task.co_channels.add_co_pulse_chan_time(f"{h.daq_device}/{counter}",
                            idle_state=Level.LOW, initial_delay=delay, high_time=width, low_time=period-width)
                channel.co_pulse_term = terminal
                task.timing.cfg_implicit_timing(sample_mode=mode, samps_per_chan=pulses)
                task.triggers.start_trigger.cfg_dig_edge_start_trig(h.ao_start_trigger, trigger_edge=Edge.RISING)
                channels.append((channel, delay, width, period))
            for task in self.tasks:
                task.control(TaskMode.TASK_COMMIT)
            if abs(ao.timing.samp_clk_rate / h.effective_line_rate_hz - 1) > 1e-6:
                raise RuntimeError("DAQ coerced AO clock")
            for channel, delay, width, period in channels:
                for actual, expected in ((channel.co_pulse_high_time, width),
                                         (channel.co_pulse_low_time, period-width),
                                         (channel.co_pulse_time_initial_delay, delay)):
                    if abs(actual - expected) > 1e-9:
                        raise RuntimeError("DAQ coerced counter timing; schedule is not executable exactly")
        except BaseException as error:
            try:
                self.stop()
            except Exception as cleanup:
                error.add_note(f"DAQ cleanup: {cleanup}")
            raise

    def start(self):
        for task in self.tasks[1:]:
            self._started_tasks.append(task)
            task.start()
        # Mark uncertain start conservatively; cleanup must inspect generated samples.
        self.started = True
        self.energized = True
        self._started_tasks.append(self.ao)
        self.ao.start()

    def wait(self, stop_event, duration_s):
        deadline = time.monotonic() + duration_s + self.policy.frame_timeout_ms / 1000
        while not all(task.is_task_done() for task in self.tasks):
            if stop_event.wait(0.002):
                raise InterruptedError("Acquisition stopped")
            if time.monotonic() >= deadline:
                raise TimeoutError("AO/counter completion timeout")

    def stop(self):
        errors = []
        # Freeze AO first so the generated-sample count describes its held output.
        for task in self.tasks:
            if task not in self._started_tasks:
                continue
            try:
                task.stop()
            except Exception as error:
                errors.append(error)
        if self.started:
            try:
                generated = int(self.ao.out_stream.total_samp_per_chan_generated)
                if generated:
                    index = ((generated - 1) % len(self.waveform) if self.continuous
                             else min(generated, len(self.waveform)) - 1)
                    self.position = self.waveform[index].copy()
            except Exception as error:
                self.position = None
                errors.append(error)
        for task in self.tasks:
            try:
                task.close()
            except Exception as error:
                errors.append(error)
        self.tasks = []
        self._started_tasks = []
        self.ao = None
        self.started = False
        if errors:
            self.position = None  # do not guess the starting point of a park ramp
            raise ExceptionGroup("DAQ cleanup failed; position is uncertain", errors)

    def move_to(self, target):
        import nidaqmx
        from nidaqmx.constants import AcquisitionType
        from nidaqmx.stream_writers import AnalogMultiChannelWriter
        if self.tasks or self.position is None:
            raise RuntimeError("Cannot ramp from an unknown or active AO position")
        h = self.profile
        target = np.asarray(target, dtype=float)
        h.check_volts(target)
        count = self.policy.park_ramp_points
        waveform = np.vstack((self.position, transition(self.position, target, count - 2), target))
        try:
            with nidaqmx.Task() as task:
                for channel in (h.ao_x, h.ao_y):
                    task.ao_channels.add_ao_voltage_chan(f"{h.daq_device}/{channel}", min_val=h.ao_min_v, max_val=h.ao_max_v)
                task.timing.cfg_samp_clk_timing(rate=h.effective_line_rate_hz,
                    sample_mode=AcquisitionType.FINITE, samps_per_chan=count)
                written = AnalogMultiChannelWriter(task.out_stream, auto_start=False).write_many_sample(
                    np.ascontiguousarray(waveform.T), timeout=self.policy.frame_timeout_ms / 1000)
                if written != count:
                    raise RuntimeError("Short parking preload")
                self.energized = True
                task.start()
                task.wait_until_done(timeout=count/h.effective_line_rate_hz + self.policy.frame_timeout_ms/1000)
            self.position = target.copy()
        except BaseException:
            self.position = None
            raise

    def park(self):
        if self.energized:
            self.move_to(self.profile.park_volts)
