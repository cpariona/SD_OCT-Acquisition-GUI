from dataclasses import replace
import ctypes
from pathlib import Path
from threading import Event
import types
import unittest
from unittest.mock import MagicMock, patch
import numpy as np
from octacq.config import load_config, ScanRequest
from octacq.scan import plan
from octacq.hardware.daq import Daq
from octacq.hardware.camera import (Camera, IMG_ATTR_LOST_FRAMES, IMG_ATTR_ROWPIXELS,
    IMG_ATTR_ROI_WIDTH, IMG_ATTR_ROI_HEIGHT, IMG_ATTR_BITSPERPIXEL, IMG_ATTR_BYTESPERPIXEL)
from octacq.hardware.system import HardwareSystem

ROOT = Path(__file__).resolve().parents[1]


class HardwareTests(unittest.TestCase):
    def setUp(self):
        h, self.r = load_config(ROOT / "config/system.toml")
        self.h = replace(h, camera_rearm_us=40)
        self.p = ScanRequest(alines=4, bscans=2, pattern="crosshair", oce_enabled=True)

    def test_task_order_fixed_pulses_and_abort_position(self):
        tasks, starts = [], []
        def create():
            task = MagicMock()
            task.start.side_effect = lambda: starts.append(task)
            task.is_task_done.return_value = True
            task.timing.samp_clk_rate = self.h.effective_line_rate_hz
            task.out_stream.total_samp_per_chan_generated = 3
            def channel(*args, **kwargs):
                result = MagicMock()
                result.co_pulse_high_time = kwargs["high_time"]
                result.co_pulse_low_time = kwargs["low_time"]
                result.co_pulse_time_initial_delay = kwargs["initial_delay"]
                return result
            task.co_channels.add_co_pulse_chan_time.side_effect = channel
            tasks.append(task)
            return task
        constants = types.SimpleNamespace(AcquisitionType=types.SimpleNamespace(FINITE=1, CONTINUOUS=2),
                     Edge=types.SimpleNamespace(RISING=1), Level=types.SimpleNamespace(LOW=0),
                     TaskMode=types.SimpleNamespace(TASK_COMMIT=1))
        writer = MagicMock()
        writer.return_value.write_many_sample.side_effect = lambda data, **kwargs: data.shape[1]
        modules = {"nidaqmx": types.SimpleNamespace(Task=create), "nidaqmx.constants": constants,
                   "nidaqmx.stream_writers": types.SimpleNamespace(AnalogMultiChannelWriter=writer)}
        s = plan(self.p, self.h)
        block = next(s.blocks(self.r.ao_buffer_bytes))
        with patch.dict("sys.modules", modules):
            daq = Daq(self.h, self.r)
            daq.prepare(s, block)
            daq.start()
            self.assertEqual(starts, [tasks[1], tasks[2], tasks[0]])
            settings = tasks[2].co_channels.add_co_pulse_chan_time.call_args.kwargs
            self.assertAlmostEqual(settings["high_time"], self.h.oce_pulse_width_us * 1e-6)
            self.assertAlmostEqual(settings["low_time"] + settings["high_time"], s.period_s * 2)
            self.assertEqual(tasks[2].timing.cfg_implicit_timing.call_args.kwargs["samps_per_chan"], 2)
            daq.stop()
            np.testing.assert_array_equal(daq.position, block.volts[2])
            self.assertTrue(all(t.close.called for t in tasks))
            # Failure while constructing the second task must still close AO.
            tasks.clear()
            with patch.object(modules["nidaqmx"], "Task", side_effect=[create(), RuntimeError("allocation")]):
                with self.assertRaisesRegex(RuntimeError, "allocation"):
                    Daq(self.h, self.r).prepare(s, block)
            self.assertTrue(tasks[0].close.called)

    def test_camera_copy_stride_release_and_integrity(self):
        camera = Camera(self.h, self.r)
        camera.session_id = ctypes.c_uint32(1)
        camera.height = 2
        camera.row_pixels = self.h.spectral_samples + 2
        raw = np.arange(2 * camera.row_pixels, dtype=np.uint16).reshape(2, -1)
        dll = MagicMock()
        camera._dll = dll
        def examine(session, wanted, actual, address):
            actual._obj.value = wanted
            address._obj.value = raw.ctypes.data
            return 0
        dll.imgSessionExamineBuffer2.side_effect = examine
        dll.imgSessionReleaseBuffer.return_value = 0
        def attribute(session, key, value):
            value._obj.value = 0
            return 0
        dll.imgGetAttribute.side_effect = attribute
        number, result = camera.read()
        self.assertEqual(number, 0)
        np.testing.assert_array_equal(result, raw[:, :self.h.spectral_samples])
        self.assertFalse(np.shares_memory(raw, result))
        def discontinuous(session, wanted, actual, address):
            actual._obj.value = wanted + 1
            return 0
        dll.imgSessionExamineBuffer2.side_effect = discontinuous
        with self.assertRaisesRegex(RuntimeError, "Discontinuous"):
            camera.read()
        self.assertEqual(dll.imgSessionReleaseBuffer.call_count, 2)
        dll.imgSessionExamineBuffer2.side_effect = examine
        def lost(session, key, value):
            value._obj.value = 1 if key == IMG_ATTR_LOST_FRAMES else 0
            return 0
        dll.imgGetAttribute.side_effect = lost
        with self.assertRaisesRegex(RuntimeError, "lost"):
            camera.read()
        self.assertEqual(camera.lost_buffers, 1)

    def test_camera_configuration_reuses_session_and_rebuilds_changed_roi(self):
        camera = Camera(self.h, self.r)
        dll = MagicMock()
        camera._dll = dll
        height = [0]
        settings = {}
        calls = []
        def interface(name, result):
            result._obj.value = 11
            return 0
        def session(interface_id, result):
            result._obj.value = 12
            return 0
        def roi(sid, top, left, rows, width):
            height[0] = rows
            return 0
        def attribute(sid, key, result):
            result._obj.value = {IMG_ATTR_ROWPIXELS: self.h.spectral_samples,
                IMG_ATTR_ROI_WIDTH: self.h.spectral_samples, IMG_ATTR_ROI_HEIGHT: height[0],
                IMG_ATTR_BITSPERPIXEL: self.h.bit_depth, IMG_ATTR_BYTESPERPIXEL: 2,
                IMG_ATTR_LOST_FRAMES: 0}[key]
            return 0
        def set_string(sid, name, value):
            settings[name] = value
            calls.append(name)
            return 0
        def get_string(sid, name, result, length):
            result.value = settings[name]
            return 0
        def set_numeric(sid, name, value):
            settings[name] = value.value
            return 0
        def get_numeric(sid, name, result):
            result._obj.value = settings[name]
            return 0
        for name, effect in dict(imgInterfaceOpen=interface, imgSessionOpen=session,
                imgSessionConfigureROI=roi, imgGetAttribute=attribute,
                imgSetCameraAttributeString=set_string, imgGetCameraAttributeString=get_string,
                imgSetCameraAttributeNumeric=set_numeric, imgGetCameraAttributeNumeric=get_numeric).items():
            getattr(dll, name).side_effect = effect
        for name in ("imgSetAttributeFromVoidPtr", "imgSessionTriggerConfigure2", "imgRingSetup",
                     "imgSessionStopAcquisition", "imgClose"):
            getattr(dll, name).return_value = 0
            getattr(dll, name).__name__ = name
        camera.connect(4)
        camera.next_buffer = 7
        camera.connect(4)
        self.assertEqual(camera.next_buffer, 7)
        self.assertEqual(dll.imgInterfaceOpen.call_count, 1)
        self.assertLess(calls.index(b"Trigger Polarity"), calls.index(b"Trigger Mode"))
        self.assertEqual(dll.imgSessionTriggerConfigure2.call_args.args[-2:], (0xFFFFFFFF, 3))
        camera.connect(8)
        self.assertEqual(dll.imgInterfaceOpen.call_count, 2)
        self.assertEqual(camera.next_buffer, 0)
        camera.close()
        self.assertEqual(camera.session_id.value, 0)

    def test_session_reuse_and_error_cleanup(self):
        system = HardwareSystem(self.h, self.r)
        camera, daq = MagicMock(), MagicMock()
        camera.lost_buffers = camera.discontinuous_buffers = 0
        system.camera, system.daq = camera, daq
        s = plan(self.p, self.h)
        data = np.zeros((self.p.frame_lines, self.h.spectral_samples), dtype=np.uint16)
        camera.read.side_effect = [(i, data) for i in range(self.p.frame_count * 2)]
        system.connect(self.p.frame_lines)
        first = list(system.execute(s, Event()))
        second = list(system.execute(s, Event()))
        self.assertEqual([item[1] for item in first + second], list(range(self.p.frame_count * 2)))
        self.assertFalse(camera.close.called)
        self.assertEqual(daq.prepare.call_count, 2)
        camera.read.side_effect = RuntimeError("discontinuity")
        with self.assertRaisesRegex(RuntimeError, "discontinuity"):
            list(system.execute(s, Event()))
        self.assertFalse(system.connected)
        self.assertTrue(camera.close.called)
        self.assertTrue(daq.park.called)

    def test_preflight_failure_never_energizes_daq(self):
        system = HardwareSystem(self.h, self.r)
        system.camera, system.daq = MagicMock(), MagicMock()
        system.camera.connect.side_effect = RuntimeError("camera configuration")
        with self.assertRaisesRegex(RuntimeError, "camera configuration"):
            system.connect(self.p.frame_lines)
        system.daq.prepare.assert_not_called()
        system.daq.start.assert_not_called()
        system.daq.park.assert_not_called()

    def test_missing_rearm_preflight_never_prepares_daq(self):
        hardware = replace(self.h, camera_rearm_us=None)
        system = HardwareSystem(hardware, self.r)
        system.camera, system.daq = MagicMock(), MagicMock()
        system.connected = True
        schedule = plan(self.p, hardware)
        with self.assertRaisesRegex(ValueError, "camera_rearm_us"):
            system.preflight(schedule)
        system.daq.prepare.assert_not_called()
        system.daq.start.assert_not_called()
