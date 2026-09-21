from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
import tomllib
import numpy as np
from octacq.config import load_config, ScanRequest
from octacq.scan import plan
from octacq.storage import RawWriter, build_header, read_info, read_raw, PREFIX

ROOT = Path(__file__).resolve().parents[1]


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.h, self.runtime = load_config(ROOT / "config/system.toml")
        self.p = ScanRequest(alines=4, bscans=2, m_repetitions=2, sync_points=3)

    def test_configuration_and_safety(self):
        source = tomllib.loads((ROOT / "config/system.toml").read_text())
        self.assertEqual(self.h.x_v_per_mm, source["galvo"]["x_v_per_mm"])
        self.assertEqual(self.h.wavelength_start_nm, source["spectrometer"]["wavelength_start_nm"])
        self.assertFalse(hasattr(self.h, "ring_buffers"))
        for kwargs in (dict(x_v_per_mm=float("nan")), dict(max_abs_v=100), dict(park_y_mm=-100)):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                replace(self.h, **kwargs)
        with self.assertRaises(ValueError):
            plan(replace(self.p, center_x_mm=100), self.h)

    def test_geometry_and_order(self):
        for mode in ("BM", "MB"):
            for pattern in ("raster", "crosshair", "meridians", "linear"):
                p = replace(self.p, mode=mode, pattern=pattern, center_x_mm=0.2, center_y_mm=0.3)
                s = plan(p, self.h)
                frames = list(s.frames())
                self.assertEqual(len(frames), p.frame_count)
                self.assertEqual(sum(len(f.xy_mm) for f in frames), p.expected_alines)
                if mode == "MB":
                    self.assertTrue(all(np.all(f.xy_mm == f.xy_mm[0]) for f in frames))
                blocks = list(s.blocks(self.runtime.ao_buffer_bytes))
                for f in frames:
                    start = f.index * s.period_ticks + p.sync_points
                    np.testing.assert_allclose(blocks[0].volts[start:start+p.frame_lines],
                                               f.xy_mm * (self.h.x_v_per_mm, self.h.y_v_per_mm))
        vertical = list(plan(replace(self.p, pattern="linear", orientation="vertical"), self.h).frames())
        self.assertTrue(np.all(vertical[0].xy_mm[:, 0] == 0))
        cross = list(plan(replace(self.p, pattern="crosshair", oce_enabled=True), self.h).frames())
        self.assertEqual([f.oce for f in cross], [True, False] * 4)
        radial = list(plan(replace(self.p, pattern="meridians", m_repetitions=1), self.h).frames())
        np.testing.assert_allclose(radial[1].xy_mm[:, 0], 0, atol=1e-15)

    def test_bidirectional_axes(self):
        p = replace(self.p, pattern="linear")
        frames = list(plan(p, self.h).frames())
        np.testing.assert_allclose(frames[0].xy_mm, frames[1].xy_mm[::-1])
        self.assertTrue(frames[1].reverse_storage)
        frames = list(plan(replace(p, mode="MB"), self.h).frames())
        self.assertGreater(frames[p.alines].xy_mm[0, 0], frames[0].xy_mm[0, 0])
        self.assertFalse(any(f.reverse_storage for f in frames))
        frames = list(plan(replace(self.p, raster_bidirectional=True), self.h).frames())
        self.assertTrue(frames[2].reverse_storage)

    def test_timing_hold_and_block_order(self):
        h = replace(self.h, camera_rearm_us=40)
        p = replace(self.p, pattern="crosshair", oce_enabled=True, bframes_delay_us=1000)
        s = plan(p, h)
        self.assertGreaterEqual(s.period_s + 1e-12, s.oce_delay_s + h.oce_pulse_width_us * 1e-6)
        self.assertEqual(s.oce_stride, 2)
        self.assertGreater(s.hold_points, 0)
        blocks = list(s.blocks(s.period_ticks * 16 * 2))
        self.assertEqual([f.index for block in blocks for f in block.frames], list(range(p.frame_count)))
        self.assertTrue(all(len(block.frames) == 2 for block in blocks))
        with self.assertRaises(ValueError):
            list(plan(replace(self.p, sync_points=0), h).blocks(self.runtime.ao_buffer_bytes))

    def test_stationary_and_cyclic_join(self):
        p = ScanRequest(alines=1, mode="MB", pattern="stationary", x_length_mm=0,
                        y_length_mm=0, m_repetitions=4, sync_points=0)
        s = plan(p, self.h, continuous=True)
        self.assertTrue(np.all(next(s.blocks(self.runtime.ao_buffer_bytes)).volts == self.h.park_volts))
        p = replace(self.p, pattern="crosshair", bscans=1, m_repetitions=1)
        s = plan(p, self.h, continuous=True)
        block = next(s.blocks(self.runtime.ao_buffer_bytes))
        self.assertEqual(len(block.frames), 2)
        self.assertGreater(block.volts[0, 1], 0)  # joins the end of the previous Y sweep

    def test_binary_complete_and_existing_reader(self):
        s = plan(self.p, self.h)
        raw = np.arange(self.p.expected_alines * self.h.spectral_samples, dtype=np.uint16).reshape(-1, self.h.spectral_samples)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "scan.bin"
            with RawWriter(path, build_header(s)) as writer:
                writer.append(raw)
            self.assertTrue(read_info(path).complete)
            data = read_raw(path, logical=True)
            np.testing.assert_array_equal(data.reshape(raw.shape), raw)
            del data
            prefix = PREFIX.unpack(path.read_bytes()[:64])
            self.assertEqual(prefix[0], b"OCTOCE1\0")
            self.assertEqual(prefix[8], 65536)
            with self.assertRaises(FileExistsError):
                RawWriter(path, build_header(s)).open()

    def test_incomplete_crash_prefix_and_crc(self):
        s = plan(self.p, self.h)
        raw = np.ones((2, self.h.spectral_samples), dtype=np.uint16)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "partial.bin"
            writer = RawWriter(path, build_header(s)).open()
            with self.assertRaises(ValueError):
                writer.append(raw.astype(float))
            writer.append(raw)
            writer.stream.close()  # process termination before JSON finalization
            writer.stream = None
            info = read_info(path)
            self.assertFalse(info.complete)
            self.assertEqual(info.committed_alines, 2)
            self.assertEqual(info.header["integrity"]["committed_alines"], 2)
            with self.assertRaises(ValueError):
                read_raw(path, logical=True)
            with path.open("r+b") as stream:
                stream.truncate(65536 + self.h.spectral_samples * 2 + 1)
            self.assertEqual(read_info(path).committed_alines, 1)
            with path.open("r+b") as stream:
                stream.seek(64)
                stream.write(b"!")
            with self.assertRaisesRegex(ValueError, "CRC"):
                read_info(path)
