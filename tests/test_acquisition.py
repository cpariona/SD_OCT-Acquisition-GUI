from dataclasses import replace
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
import numpy as np
from octacq.acquisition import Acquisition
from octacq.config import load_config, ScanRequest
from octacq.storage import read_info, read_raw

ROOT = Path(__file__).resolve().parents[1]


class Instrument:
    """Controlled raw source: markers expose ordering, never simulate OCT physics."""
    def __init__(self):
        self.connected = True
        self.statistics = {}
        self.cleaned = False
        self.fail_at = None
        self.delay = 0.005
        self.number_offset = 0

    def execute(self, schedule, stop):
        try:
            number = self.number_offset
            while True:
                for frame in schedule.frames():
                    if stop.wait(self.delay):
                        raise InterruptedError()
                    if frame.index == self.fail_at:
                        raise RuntimeError("lost camera buffer")
                    raw = np.repeat(np.arange(len(frame.xy_mm), dtype=np.uint16)[:, None],
                                    schedule.hardware.spectral_samples, axis=1)
                    yield frame, number, raw
                    number += 1
                if not schedule.continuous:
                    return
        finally:
            self.cleaned = True

    def disconnect(self):
        self.connected = False


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.h, self.r = load_config(ROOT / "config/system.toml")
        self.p = ScanRequest(alines=4, bscans=1, m_repetitions=2, pattern="linear", sync_points=3)
        self.engine = Acquisition(self.h, self.r)
        self.engine.hardware = Instrument()

    def run_file(self, path):
        self.engine.start(self.p, path)
        self.engine.join(5)
        self.assertFalse(self.engine.active)
        self.assertTrue(self.engine.hardware.cleaned)

    def test_complete_normalizes_bm_and_preserves_mb_time(self):
        with tempfile.TemporaryDirectory() as temp:
            for mode in ("BM", "MB"):
                self.p = replace(self.p, mode=mode)
                path = Path(temp) / f"{mode}.bin"
                self.run_file(path)
                self.assertEqual(self.engine.state, "completed")
                self.assertTrue(read_info(path).complete)
                raw = read_raw(path)
                expected = [0, 1, 2, 3, 3, 2, 1, 0] if mode == "BM" else [0, 1] * 4
                np.testing.assert_array_equal(raw[:, 0], expected)
                del raw

    def test_error_preserves_confirmed_prefix(self):
        self.engine.hardware.fail_at = 1
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "failure.bin"
            self.run_file(path)
            info = read_info(path)
            self.assertEqual(self.engine.state, "error")
            self.assertFalse(info.complete)
            self.assertEqual(info.committed_alines, self.p.frame_lines)
            self.assertIn("lost camera buffer", info.header["integrity"]["reason"])

    def test_stop_and_writer_error_finalize_incomplete(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "stopped.bin"
            self.engine.hardware.delay = 0.5
            self.engine.start(self.p, path)
            time.sleep(0.05)
            with self.assertRaises(RuntimeError):
                self.engine.start(self.p, path)
            self.engine.stop()
            self.engine.join(5)
            self.assertEqual(self.engine.state, "stopped")
            self.assertFalse(read_info(path).complete)
            self.engine.hardware.delay = 0.005
            path = Path(temp) / "disk.bin"
            with patch("octacq.acquisition.RawWriter.append", side_effect=OSError("disk full")):
                self.run_file(path)
            self.assertEqual(self.engine.state, "error")
            self.assertIn("disk full", read_info(path).header["integrity"]["reason"])

    def test_continuous_stops_without_output(self):
        p = replace(self.p, pattern="crosshair", m_repetitions=1)
        self.engine.start(p, None, continuous=True)
        time.sleep(0.04)
        self.engine.stop()
        self.engine.join(5)
        self.assertEqual(self.engine.state, "stopped")
        self.assertTrue(self.engine.hardware.cleaned)
