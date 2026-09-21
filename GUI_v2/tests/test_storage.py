from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from octoce.config import HardwareConfig, ScanParameters
from octoce.storage import OctBinWriter, build_header, open_memmap, read_info


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scan = ScanParameters(
            alines=4,
            bscans=2,
            m_repetitions=2,
            sync_points=1,
            x_length_mm=2.0,
            y_length_mm=1.0,
        )
        self.hardware = HardwareConfig(spectral_samples=8, sensor_bit_depth=12, oce_enabled=False)
        self.header = build_header(
            self.scan,
            self.hardware,
            backend="test",
            trajectory_sha256="0" * 64,
        )

    def test_complete_roundtrip_and_logical_shape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "complete.bin"
            expected = np.arange(self.scan.expected_alines * 8, dtype=np.uint16).reshape(-1, 8)
            writer = OctBinWriter(path, self.header).open()
            writer.append(expected[:5])
            writer.append(expected[5:])
            writer.close(complete=True)
            info = read_info(path)
            self.assertTrue(info.complete)
            self.assertEqual(info.committed_alines, self.scan.expected_alines)
            data = open_memmap(path)
            np.testing.assert_array_equal(data, expected)
            del data
            logical = open_memmap(path, logical_shape=True)
            self.assertEqual(logical.shape, (2, 2, 4, 8))
            del logical

    def test_incomplete_file_remains_readable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "partial.bin"
            block = np.full((3, 8), 77, dtype=np.uint16)
            writer = OctBinWriter(path, self.header).open()
            writer.append(block)
            writer.close(complete=False, reason="test stop")
            info = read_info(path)
            self.assertFalse(info.complete)
            self.assertEqual(info.committed_alines, 3)
            self.assertEqual(info.header["integrity"]["reason"], "test stop")
            np.testing.assert_array_equal(open_memmap(path), block)

    def test_existing_file_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "existing.bin"
            path.write_bytes(b"user data")
            with self.assertRaises(FileExistsError):
                OctBinWriter(path, self.header).open()
            self.assertEqual(path.read_bytes(), b"user data")

    def test_context_manager_marks_exception_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "failed.bin"
            with self.assertRaisesRegex(RuntimeError, "intentional"):
                with OctBinWriter(path, self.header) as writer:
                    writer.append(np.full((2, 8), 3, dtype=np.uint16))
                    raise RuntimeError("intentional")
            info = read_info(path)
            self.assertFalse(info.complete)
            self.assertEqual(info.committed_alines, 2)
            self.assertEqual(info.header["integrity"]["reason"], "intentional")


if __name__ == "__main__":
    unittest.main()
