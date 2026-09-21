from __future__ import annotations

import unittest
from threading import Event
from unittest.mock import patch

import numpy as np

from octoce.backends.base import FrameIntegrityError
from octoce.backends.ni_hardware import NIHardwareBackend
from octoce.config import AcquisitionMode, HardwareConfig, ScanParameters, ScanPattern
from octoce.scan import ScanPlanner


class _Camera:
    bits_per_pixel = 12

    def __init__(self) -> None:
        self.number = 0
        self.skip_at: int | None = None
        self.closed = False

    def open(self, **kwargs) -> None:
        self.height = kwargs["height"]
        self.width = kwargs["width"]

    def read_into(self, destination: np.ndarray) -> tuple[int, int, int]:
        assert destination.shape == (self.height, self.width)
        requested = self.number
        copied = requested + 1 if self.skip_at == requested else requested
        destination[:] = requested + 1
        self.number += 1
        return requested, copied, 0

    def close(self, *, abort: bool = False) -> None:
        self.closed = True


class _Daq:
    def __init__(self, config: HardwareConfig) -> None:
        self.started: list[tuple[int, ...]] = []
        self.waits = 0
        self.aborts = 0
        self.parked = False

    def start_mb_chunk(self, segments) -> int:
        self.started.append(tuple(s.sequence_index for s in segments))
        return segments[0].ticks

    def wait_segment(self, stop_event: Event, timeout_s: float) -> None:
        self.waits += 1

    def abort_segment(self) -> None:
        self.aborts += 1

    def park(self) -> None:
        self.parked = True


class OptimizedHardwareTests(unittest.TestCase):
    def tearDown(self) -> None:
        NIHardwareBackend.release_warm_camera()

    def setUp(self) -> None:
        self.scan = ScanParameters(
            alines=3, bscans=1, m_repetitions=4, sync_points=2,
            mode=AcquisitionMode.MB,
        )
        self.hardware = HardwareConfig(spectral_samples=16, line_rate_hz=10_000.0)
        self.segments = list(ScanPlanner(self.scan, self.hardware).iter_segments())

    def test_camera_is_opened_once_and_frames_remain_ordered(self) -> None:
        with patch("octoce.backends.ni_hardware.NIIMAQCamera", _Camera), patch(
            "octoce.backends.ni_hardware.NIDaqGalvoController", _Daq
        ):
            backend = NIHardwareBackend()
            self.assertTrue(backend.supports_mb_chunks(self.scan))
            backend.open(self.scan, self.hardware)
            received = list(backend.acquire_mb_chunk(self.segments, Event()))
            self.assertEqual(backend._daq.started, [(0, 1, 2)])
            self.assertEqual(backend._daq.waits, 1)
            self.assertEqual([result.copied_buffer for _, _, result in received], [0, 1, 2])
            self.assertTrue(all(np.all(data == index + 1) for index, (_, data, _) in enumerate(received)))
            backend.close()
            self.assertTrue(backend._camera is None)

    def test_camera_buffer_discontinuity_aborts_chunk(self) -> None:
        with patch("octoce.backends.ni_hardware.NIIMAQCamera", _Camera), patch(
            "octoce.backends.ni_hardware.NIDaqGalvoController", _Daq
        ):
            backend = NIHardwareBackend()
            backend.open(self.scan, self.hardware)
            daq = backend._daq
            backend._camera.skip_at = 1
            with self.assertRaises(FrameIntegrityError):
                list(backend.acquire_mb_chunk(self.segments, Event()))
            self.assertGreater(daq.aborts, 0)
            backend.close(abort=True)

    def test_warm_imaq_reuses_session_only_for_matching_camera_configuration(self) -> None:
        with patch("octoce.backends.ni_hardware.NIIMAQCamera", _Camera), patch(
            "octoce.backends.ni_hardware.NIDaqGalvoController", _Daq
        ):
            first = NIHardwareBackend(warm_camera=True)
            first.open(self.scan, self.hardware)
            camera = first._camera
            first.close()
            second = NIHardwareBackend(warm_camera=True)
            second.open(self.scan, self.hardware)
            self.assertIs(second._camera, camera)
            self.assertFalse(camera.closed)
            second.close()
            changed = ScanParameters(
                alines=5, bscans=1, m_repetitions=4, sync_points=2,
                mode=AcquisitionMode.BM,
            )
            third = NIHardwareBackend(warm_camera=True)
            third.open(changed, self.hardware)
            self.assertIsNot(third._camera, camera)
            self.assertTrue(camera.closed)
            third.close(abort=True)

    def test_sync_zero_keeps_conservative_path(self) -> None:
        zero_sync = ScanParameters(
            alines=1, bscans=1, m_repetitions=1000, sync_points=0,
            x_length_mm=0.0, y_length_mm=0.0, mode=AcquisitionMode.MB,
        )
        self.assertFalse(NIHardwareBackend().supports_mb_chunks(zero_sync))

    def test_stationary_mb_sync_zero_many_positions_uses_chunks(self) -> None:
        scan = ScanParameters(
            alines=2, bscans=1, m_repetitions=1000, sync_points=0,
            x_length_mm=0.0, y_length_mm=0.0, mode=AcquisitionMode.MB,
        )
        self.assertTrue(NIHardwareBackend().supports_mb_chunks(scan))

    def test_bm_crosshair_chunk_size_keeps_xy_pairs(self) -> None:
        scan = ScanParameters(
            alines=500, bscans=3, m_repetitions=1, sync_points=50,
            mode=AcquisitionMode.BM, pattern=ScanPattern.CROSSHAIR,
        )
        self.assertTrue(NIHardwareBackend().supports_mb_chunks(scan))
        self.assertEqual(NIHardwareBackend.mb_chunk_size(scan, self.hardware) % 2, 0)


if __name__ == "__main__":
    unittest.main()
