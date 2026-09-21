from __future__ import annotations

import sys
import types
import unittest
from threading import Event
from unittest.mock import patch

import numpy as np

from octoce.backends.ni_daq import NIDaqGalvoController
from octoce.config import HardwareConfig, ScanParameters
from octoce.scan import ScanPlanner


class _Channel:
    def __init__(self, **settings: object):
        self.settings = settings
        self.co_pulse_term = ""


class _AOChannels:
    def __init__(self, task: "_Task"):
        self.task = task

    def add_ao_voltage_chan(self, channel: str, **settings: object) -> _Channel:
        self.task.channel = channel
        return _Channel(**settings)


class _COChannels:
    def __init__(self, task: "_Task"):
        self.task = task

    def add_co_pulse_chan_time(self, counter: str, **settings: object) -> _Channel:
        self.task.counter = counter
        self.task.pulse_settings = settings
        self.task.channel_object = _Channel(**settings)
        return self.task.channel_object


class _Timing:
    def __init__(self, task: "_Task"):
        self.task = task
        self.samp_clk_rate = 0.0

    def cfg_samp_clk_timing(self, *, rate: float, **settings: object) -> None:
        self.samp_clk_rate = rate
        self.task.sample_settings = {"rate": rate, **settings}

    def cfg_implicit_timing(self, **settings: object) -> None:
        self.task.sample_settings = settings


class _StartTrigger:
    def __init__(self, task: "_Task"):
        self.task = task

    def cfg_dig_edge_start_trig(self, source: str, **settings: object) -> None:
        self.task.trigger_settings = {"source": source, **settings}


class _Task:
    registry: list["_Task"] = []
    start_log: list[str] = []

    def __init__(self, name: str):
        self.name = name
        self.ao_channels = _AOChannels(self)
        self.co_channels = _COChannels(self)
        self.timing = _Timing(self)
        self.triggers = types.SimpleNamespace(start_trigger=_StartTrigger(self))
        self.out_stream = self
        self.closed = False
        self.done_checks = 0
        self.done_after_checks = 1
        self.written: np.ndarray | None = None
        self.__class__.registry.append(self)

    def control(self, mode: object) -> None:
        self.control_mode = mode

    def start(self) -> None:
        self.__class__.start_log.append(self.name)

    def is_task_done(self) -> bool:
        self.done_checks += 1
        return self.done_checks >= self.done_after_checks

    def wait_until_done(self, timeout: float) -> None:
        self.wait_timeout = timeout

    def stop(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _Writer:
    def __init__(self, out_stream: _Task, auto_start: bool):
        self.task = out_stream
        self.auto_start = auto_start

    def write_many_sample(self, data: np.ndarray, timeout: float) -> int:
        self.task.written = np.array(data, copy=True)
        return int(data.shape[1])


def _fake_modules() -> dict[str, types.ModuleType]:
    nidaqmx = types.ModuleType("nidaqmx")
    nidaqmx.Task = _Task  # type: ignore[attr-defined]
    constants = types.ModuleType("nidaqmx.constants")
    constants.AcquisitionType = types.SimpleNamespace(FINITE="finite", CONTINUOUS="continuous")
    constants.Edge = types.SimpleNamespace(RISING="rising")
    constants.Level = types.SimpleNamespace(LOW="low")
    constants.TaskMode = types.SimpleNamespace(TASK_COMMIT="commit")
    writers = types.ModuleType("nidaqmx.stream_writers")
    writers.AnalogMultiChannelWriter = _Writer  # type: ignore[attr-defined]
    return {
        "nidaqmx": nidaqmx,
        "nidaqmx.constants": constants,
        "nidaqmx.stream_writers": writers,
    }


class NIDaqSynchronizationTests(unittest.TestCase):
    def setUp(self) -> None:
        _Task.registry.clear()
        _Task.start_log.clear()

    def test_consumers_are_armed_before_ao_master(self) -> None:
        scan = ScanParameters(
            alines=3,
            bscans=1,
            m_repetitions=1,
            sync_points=2,
            bframes_delay_us=1.5,
        )
        hardware = HardwareConfig(line_rate_hz=100_000.0)
        segment = next(ScanPlanner(scan, hardware).iter_segments())
        with patch.dict(sys.modules, _fake_modules()):
            controller = NIDaqGalvoController(hardware)
            controller.start_segment(segment)
            self.assertEqual(
                _Task.start_log,
                ["octoce_camera_trigger", "octoce_oce_trigger", "octoce_ao"],
            )
            tasks = {task.name: task for task in _Task.registry}
            camera = tasks["octoce_camera_trigger"]
            oce = tasks["octoce_oce_trigger"]
            ao = tasks["octoce_ao"]
            self.assertAlmostEqual(camera.pulse_settings["initial_delay"], 20e-6)
            self.assertEqual(camera.sample_settings["samps_per_chan"], 1)
            self.assertEqual(camera.channel_object.co_pulse_term, "/Dev1/PFI12")
            self.assertEqual(oce.sample_settings["samps_per_chan"], 1)
            self.assertEqual(oce.channel_object.co_pulse_term, "/Dev1/PFI13")
            self.assertAlmostEqual(oce.pulse_settings["initial_delay"], 21.5e-6)
            self.assertAlmostEqual(oce.pulse_settings["high_time"], 10e-6)
            self.assertAlmostEqual(oce.pulse_settings["low_time"], 90e-6)
            self.assertEqual(ao.written.shape, (2, segment.ticks))
            oce.done_after_checks = 3
            controller.wait_segment(Event(), timeout_s=1.0)
            self.assertGreaterEqual(oce.done_checks, 3)
            self.assertTrue(all(task.closed for task in (camera, oce, ao)))

    def test_park_does_not_energize_ao_before_first_segment(self) -> None:
        hardware = HardwareConfig()
        with patch.dict(sys.modules, _fake_modules()):
            controller = NIDaqGalvoController(hardware)
            controller.park()
        self.assertEqual(_Task.registry, [])
        self.assertEqual(_Task.start_log, [])

    def test_continuous_alignment_uses_persistent_synchronized_counters(self) -> None:
        hardware = HardwareConfig(line_rate_hz=52_500.0)
        with patch.dict(sys.modules, _fake_modules()):
            controller = NIDaqGalvoController(hardware)
            controller.start_continuous_alignment(alines_per_block=1000, block_rate_hz=50.0)
            self.assertEqual(
                _Task.start_log,
                ["octoce_alignment_camera_50hz", "octoce_alignment_oce_50hz", "octoce_alignment_ao_hold"],
            )
            tasks = {task.name: task for task in _Task.registry}
            camera = tasks["octoce_alignment_camera_50hz"]
            oce = tasks["octoce_alignment_oce_50hz"]
            ao = tasks["octoce_alignment_ao_hold"]
            self.assertEqual(camera.sample_settings["sample_mode"], "continuous")
            self.assertEqual(oce.sample_settings["sample_mode"], "continuous")
            self.assertEqual(camera.trigger_settings["source"], "/Dev1/ao/StartTrigger")
            self.assertEqual(oce.trigger_settings["source"], "/Dev1/ao/StartTrigger")
            self.assertAlmostEqual(camera.pulse_settings["initial_delay"], 0.02)
            self.assertAlmostEqual(oce.pulse_settings["initial_delay"], 0.02)
            self.assertAlmostEqual(oce.pulse_settings["high_time"], 0.002)
            self.assertAlmostEqual(oce.pulse_settings["low_time"], 0.018)
            np.testing.assert_array_equal(ao.written, np.zeros((2, 2)))
            controller.abort_segment()
            self.assertTrue(all(task.closed for task in (camera, oce, ao)))

    def test_crosshair_y_segment_does_not_repeat_bm_oce_trigger(self) -> None:
        from octoce.config import ScanPattern

        scan = ScanParameters(
            alines=3,
            bscans=1,
            m_repetitions=1,
            sync_points=2,
            pattern=ScanPattern.CROSSHAIR,
        )
        hardware = HardwareConfig(line_rate_hz=100_000.0)
        y_segment = list(ScanPlanner(scan, hardware).iter_segments())[1]
        self.assertEqual(y_segment.oce_trigger_count, 0)
        with patch.dict(sys.modules, _fake_modules()):
            controller = NIDaqGalvoController(hardware)
            controller.start_segment(y_segment)
            self.assertEqual(_Task.start_log, ["octoce_camera_trigger", "octoce_ao"])
            self.assertNotIn("octoce_oce_trigger", {task.name for task in _Task.registry})
            controller.wait_segment(Event(), timeout_s=1.0)


if __name__ == "__main__":
    unittest.main()
