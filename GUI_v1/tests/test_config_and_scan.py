from __future__ import annotations

import unittest

import numpy as np

from octoce.config import (
    AcquisitionMode, HardwareConfig, Orientation, ScanParameters, ScanPattern,
    stationary_alignment_timing,
)
from octoce.scan import ScanPlanner, quintic_transition


class ScanPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.hardware = HardwareConfig(spectral_samples=64, oce_enabled=False)

    def test_stationary_alignment_reserves_camera_idle_time_at_50_hz(self) -> None:
        original = HardwareConfig()
        capture, block_rate_hz = stationary_alignment_timing(original, 1000)
        self.assertAlmostEqual(block_rate_hz, 50.0)
        self.assertAlmostEqual(capture.effective_line_rate_hz, 52_631.5789, places=3)
        self.assertGreater(
            1 / block_rate_hz - 1000 / capture.effective_line_rate_hz,
            0.0009,
        )
        self.assertEqual(original.line_rate_hz, 50_000.0)

    def test_bm_order_and_shape(self) -> None:
        scan = ScanParameters(
            alines=5,
            bscans=3,
            m_repetitions=2,
            sync_points=4,
            x_length_mm=4.0,
            y_length_mm=2.0,
            mode=AcquisitionMode.BM,
            pattern=ScanPattern.RASTER,
        )
        segments = list(ScanPlanner(scan, self.hardware).iter_segments())
        self.assertEqual(len(segments), 6)
        self.assertEqual(scan.axis_order, ("bscan", "m_repetition", "aline", "pixel"))
        self.assertEqual((segments[0].bscan_index, segments[0].repetition_index), (0, 0))
        self.assertEqual((segments[1].bscan_index, segments[1].repetition_index), (0, 1))
        self.assertEqual(segments[0].xy_mm.shape, (9, 2))
        self.assertEqual(segments[0].active_start, 4)
        self.assertEqual(segments[0].active_count, 5)
        self.assertEqual(sum(segment.oce_trigger_count for segment in segments), 6)
        np.testing.assert_allclose(segments[0].active_xy_mm[:, 0], [-2, -1, 0, 1, 2])
        np.testing.assert_allclose(segments[0].active_xy_mm[:, 1], -1.0)

    def test_mb_order_and_stationary_repetitions(self) -> None:
        scan = ScanParameters(
            alines=4,
            bscans=2,
            m_repetitions=3,
            sync_points=2,
            x_length_mm=3.0,
            y_length_mm=1.0,
            mode=AcquisitionMode.MB,
            pattern=ScanPattern.RASTER,
        )
        segments = list(ScanPlanner(scan, self.hardware).iter_segments())
        self.assertEqual(len(segments), 8)
        self.assertEqual(scan.axis_order, ("bscan", "aline", "m_repetition", "pixel"))
        self.assertEqual((segments[0].bscan_index, segments[0].aline_index), (0, 0))
        self.assertEqual((segments[1].bscan_index, segments[1].aline_index), (0, 1))
        self.assertEqual(segments[0].xy_mm.shape, (5, 2))
        expected = np.repeat(segments[0].active_xy_mm[:1], 3, axis=0)
        np.testing.assert_allclose(segments[0].active_xy_mm, expected)

    def test_crosshair_contains_x_and_y_in_each_logical_bscan(self) -> None:
        scan = ScanParameters(
            alines=5,
            bscans=2,
            m_repetitions=1,
            sync_points=0,
            x_length_mm=4.0,
            y_length_mm=6.0,
            pattern=ScanPattern.CROSSHAIR,
        )
        planner = ScanPlanner(scan, self.hardware)
        lines = planner.overview_lines()
        self.assertEqual(len(lines), 4)
        np.testing.assert_allclose(lines[0][:, 1], 0.0)
        np.testing.assert_allclose(lines[1][:, 0], 0.0)
        self.assertAlmostEqual(float(np.ptp(lines[0][:, 0])), 4.0)
        self.assertAlmostEqual(float(np.ptp(lines[1][:, 1])), 6.0)
        segments = list(planner.iter_segments())
        self.assertEqual(len(segments), 4)
        self.assertEqual(scan.expected_alines, 20)
        self.assertEqual(scan.axis_order, ("bscan", "m_repetition", "sweep_xy", "aline", "pixel"))
        self.assertEqual(scan.logical_shape_without_pixels, (2, 1, 2, 5))
        self.assertEqual([segment.oce_trigger_count for segment in segments], [1, 0, 1, 0])

    def test_vertical_linear(self) -> None:
        scan = ScanParameters(
            alines=4,
            bscans=1,
            x_length_mm=0.0,
            y_length_mm=8.0,
            pattern=ScanPattern.LINEAR,
            orientation=Orientation.VERTICAL,
        )
        line = ScanPlanner(scan, self.hardware).overview_lines()[0]
        np.testing.assert_allclose(line[:, 0], 0.0)
        self.assertAlmostEqual(float(np.ptp(line[:, 1])), 8.0)

    def test_linear_bm_alternates_every_m_sweep_without_flyback(self) -> None:
        for orientation, axis, length in (
            (Orientation.HORIZONTAL, 0, 4.0),
            (Orientation.VERTICAL, 1, 6.0),
        ):
            with self.subTest(orientation=orientation):
                scan = ScanParameters(
                    alines=5, bscans=2, m_repetitions=3, sync_points=4,
                    x_length_mm=4.0, y_length_mm=6.0,
                    mode=AcquisitionMode.BM, pattern=ScanPattern.LINEAR,
                    orientation=orientation,
                )
                segments = list(ScanPlanner(scan, self.hardware).iter_segments())
                self.assertEqual(len(segments), 6)
                self.assertTrue(scan.to_dict()["linear_bidirectional"])
                forward = np.linspace(-length / 2, length / 2, 5)
                for index, segment in enumerate(segments):
                    expected = forward[::-1] if index % 2 else forward
                    np.testing.assert_allclose(segment.active_xy_mm[:, axis], expected)
                    self.assertEqual(segment.logical_reverse, bool(index % 2))
                    if index:
                        np.testing.assert_allclose(
                            segment.xy_mm[:scan.sync_points],
                            np.repeat(segments[index - 1].active_xy_mm[-1:], scan.sync_points, axis=0),
                        )

    def test_linear_mb_reverses_position_order_not_m_time_axis(self) -> None:
        scan = ScanParameters(
            alines=4, bscans=2, m_repetitions=3, sync_points=2,
            x_length_mm=4.0, y_length_mm=0.0,
            mode=AcquisitionMode.MB, pattern=ScanPattern.LINEAR,
        )
        segments = list(ScanPlanner(scan, self.hardware).iter_segments())
        self.assertEqual(len(segments), 8)
        first = [float(s.active_xy_mm[0, 0]) for s in segments[:4]]
        second = [float(s.active_xy_mm[0, 0]) for s in segments[4:]]
        np.testing.assert_allclose(first, np.linspace(-2, 2, 4))
        np.testing.assert_allclose(second, np.linspace(2, -2, 4))
        self.assertEqual([s.aline_index for s in segments[4:]], [0, 1, 2, 3])
        self.assertFalse(any(s.logical_reverse for s in segments))
        for segment in segments:
            np.testing.assert_allclose(
                segment.active_xy_mm,
                np.repeat(segment.active_xy_mm[:1], scan.m_repetitions, axis=0),
            )
        np.testing.assert_allclose(
            segments[4].xy_mm[:scan.sync_points],
            np.repeat(segments[3].active_xy_mm[-1:], scan.sync_points, axis=0),
        )

    def test_zero_by_zero_is_valid_stationary_center(self) -> None:
        for pattern in ScanPattern:
            with self.subTest(pattern=pattern):
                scan = ScanParameters(
                    alines=1, bscans=1, m_repetitions=4, sync_points=3,
                    x_length_mm=0.0, y_length_mm=0.0,
                    mode=AcquisitionMode.MB, pattern=pattern,
                )
                self.assertTrue(scan.is_stationary)
                segments = list(ScanPlanner(scan, self.hardware).iter_segments())
                self.assertTrue(segments)
                for segment in segments:
                    np.testing.assert_allclose(segment.active_xy_mm, 0.0)
                    np.testing.assert_allclose(segment.active_xy_volts, 0.0)

    def test_meridians_are_half_turn_unique_diameters(self) -> None:
        scan = ScanParameters(
            alines=3,
            bscans=4,
            x_length_mm=4.0,
            y_length_mm=4.0,
            pattern=ScanPattern.MERIDIANS,
        )
        lines = ScanPlanner(scan, self.hardware).overview_lines()
        np.testing.assert_allclose(lines[0][:, 1], 0.0, atol=1e-12)
        np.testing.assert_allclose(lines[2][:, 0], 0.0, atol=1e-12)

    def test_conversion_uses_confirmed_precise_calibration(self) -> None:
        scan = ScanParameters(
            alines=3,
            bscans=1,
            x_length_mm=2.0,
            y_length_mm=0.0,
            pattern=ScanPattern.LINEAR,
        )
        segment = next(ScanPlanner(scan, self.hardware).iter_segments())
        np.testing.assert_allclose(segment.active_xy_volts[:, 0], [-0.2364067, 0.0, 0.2364067])

    def test_mb_crosshair_triggers_once_per_group_of_m_alines(self) -> None:
        scan = ScanParameters(
            alines=3,
            bscans=1,
            m_repetitions=4,
            sync_points=2,
            x_length_mm=2.0,
            y_length_mm=2.0,
            mode=AcquisitionMode.MB,
            pattern=ScanPattern.CROSSHAIR,
            bframes_delay_us=1.5,
        )
        segments = list(ScanPlanner(scan, self.hardware).iter_segments())
        self.assertEqual(len(segments), 6)
        self.assertTrue(all(segment.active_count == 4 for segment in segments))
        self.assertTrue(all(segment.oce_trigger_count == 1 for segment in segments))
        self.assertTrue(all(segment.bframes_delay_us == 1.5 for segment in segments))
        self.assertEqual(scan.axis_order, ("bscan", "sweep_xy", "aline", "m_repetition", "pixel"))
        self.assertEqual(scan.logical_shape_without_pixels, (1, 2, 3, 4))

    def test_camera_rate_is_limited_to_147_klps(self) -> None:
        scan = ScanParameters(alines=64, bscans=1, sync_points=50)
        maximum = HardwareConfig(line_rate_hz=147_000.0)
        maximum.validate(scan)
        self.assertEqual(maximum.cc1_period_us, 6.8)
        self.assertAlmostEqual(maximum.effective_line_rate_hz, 1_000_000.0 / 6.8)
        self.assertEqual(maximum.camera_operational_setting, "OPR 0 - 115 to 147klps")
        with self.assertRaisesRegex(ValueError, "147 klps"):
            HardwareConfig(line_rate_hz=147_001.0).validate(scan)

    def test_oce_pulse_gets_unacquired_tail_hold_at_max_rate(self) -> None:
        scan = ScanParameters(
            alines=3,
            bscans=1,
            m_repetitions=1,
            sync_points=0,
            mode=AcquisitionMode.MB,
        )
        hardware = HardwareConfig(line_rate_hz=147_000.0, oce_pulse_width_us=10.0)
        hardware.validate(scan)
        self.assertEqual(hardware.oce_post_hold_points(scan), 1)
        segment = next(ScanPlanner(scan, hardware).iter_segments())
        self.assertEqual(segment.active_count, 1)
        self.assertEqual(segment.ticks, 2)
        np.testing.assert_allclose(segment.xy_mm[0], segment.xy_mm[1])

    def test_gvs002_manual_limits_apply_when_beam_diameter_is_known(self) -> None:
        hardware = HardwareConfig(beam_diameter_mm=5.0, galvo_v_per_degree=0.8)
        safe = ScanParameters(
            alines=32,
            bscans=2,
            x_length_mm=4.0,
            y_length_mm=4.0,
        )
        hardware.validate(safe)
        unsafe_y = ScanParameters(
            alines=32,
            bscans=2,
            x_length_mm=4.0,
            y_length_mm=30.0,
        )
        with self.assertRaisesRegex(ValueError, "recorrido Y"):
            hardware.validate(unsafe_y)
        with self.assertRaisesRegex(ValueError, "recorrido Y"):
            HardwareConfig(beam_diameter_mm=0.0).validate(unsafe_y)

    def test_half_volt_per_degree_setting_caps_input_at_6_25_v(self) -> None:
        scan = ScanParameters(alines=32, bscans=2)
        with self.assertRaisesRegex(ValueError, "±6.25 V"):
            HardwareConfig(galvo_v_per_degree=0.5, max_galvo_abs_v=9.5).validate(scan)

    def test_quintic_transition_excludes_endpoints(self) -> None:
        points = quintic_transition(np.array([0.0, 0.0]), np.array([1.0, -1.0]), 4)
        self.assertEqual(points.shape, (4, 2))
        self.assertTrue(np.all(points[:, 0] > 0.0))
        self.assertTrue(np.all(points[:, 0] < 1.0))
        self.assertTrue(np.all(np.diff(points[:, 0]) > 0.0))


if __name__ == "__main__":
    unittest.main()
