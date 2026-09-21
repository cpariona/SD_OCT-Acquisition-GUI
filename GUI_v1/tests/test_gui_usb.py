from __future__ import annotations

import os
import tempfile
import threading
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from octoce.engine import EngineEvent, EngineState
from octoce.gui_usb import OCTOCEUSBApp
from octoce.usb_camera import USBCameraStream


class _FakeStream:
    def __init__(self) -> None:
        self.running = False
        self.started: list[int] = []
        self.stops = 0
        self.frame: np.ndarray | None = None
        self.recording = False
        self.recorded_paths: list[str] = []
        self.last_record_error = None

    def start(self, index: int) -> None:
        self.started.append(index)
        self.running = True

    def stop(self) -> bool:
        self.stops += 1
        self.running = False
        return True

    def snapshot(self) -> tuple[np.ndarray | None, str]:
        frame, self.frame = self.frame, None
        return frame, "USB de prueba"

    def start_recording(self, path) -> None:
        self.recorded_paths.append(str(path))
        self.recording = True

    def stop_recording(self):
        self.recording = False
        return self.recorded_paths[-1] if self.recorded_paths else None, 3


@unittest.skipUnless(os.name == "nt", "Smoke visual de Tkinter para Windows")
class USBGuiTests(unittest.TestCase):
    def test_video_is_armed_before_oct_and_closed_on_terminal_state(self) -> None:
        root = tk.Tk()
        stream = _FakeStream()
        app = OCTOCEUSBApp(root, usb_stream=stream)
        try:
            app.save_var.set(False)
            app.record_video_var.set(True)
            observed = []
            with patch.object(app.engine, "start", side_effect=lambda *args, **kwargs: observed.append(stream.recording)):
                app._start()
            self.assertEqual(observed, [True])
            self.assertTrue(stream.recording)
            self.assertEqual(len(stream.recorded_paths), 1)
            self.assertTrue(stream.recorded_paths[0].endswith(".mp4"))
            app._handle_event(EngineEvent("state", {"state": EngineState.COMPLETED.value}))
            self.assertFalse(stream.recording)
        finally:
            if root.winfo_exists():
                app._on_close()

    def test_usb_view_and_alignment_zoom_are_mutually_exclusive(self) -> None:
        root = tk.Tk()
        stream = _FakeStream()
        try:
            app = OCTOCEUSBApp(root, usb_stream=stream)
            root.update_idletasks()
            self.assertEqual(stream.started, [0])
            self.assertTrue(app.usb_panel.grid_info())
            self.assertFalse(app.alignment_zoom_canvas.grid_info())
            self.assertTrue(app.crosshair_loop_button.winfo_exists())
            self.assertEqual(app.engine.preview_depth_range, (1, 2048))

            with patch.object(app.engine, "start") as start:
                app._start_crosshair_loop()
            self.assertTrue(start.call_args.kwargs["continuous"])
            self.assertTrue(app.usb_panel.grid_info())
            self.assertTrue(stream.running)

            stream.frame = np.zeros((48, 64, 3), dtype=np.uint8)
            app._poll_usb()
            self.assertTrue(any(app.usb_canvas.type(item) == "image" for item in app.usb_canvas.find_all()))

            app._alignment_active = True
            app._clear_preview("alineacion")
            app.alignment_zoom_canvas.grid()
            self.assertFalse(app.usb_panel.grid_info())
            self.assertTrue(app.alignment_zoom_canvas.grid_info())
            self.assertFalse(stream.running)

            app._handle_event(EngineEvent("state", {"state": EngineState.STOPPED.value, "reason": "prueba"}))
            self.assertTrue(app.usb_panel.grid_info())
            self.assertFalse(app.alignment_zoom_canvas.grid_info())
            self.assertEqual(stream.started, [0, 0])
        finally:
            if root.winfo_exists():
                app._on_close()


class _FakeCapture:
    def __init__(self) -> None:
        self.released = threading.Event()

    def isOpened(self) -> bool:
        return True

    def read(self) -> tuple[bool, np.ndarray]:
        time.sleep(0.01)
        frame = np.zeros((4, 6, 3), dtype=np.uint8)
        frame[:, :, 0] = 255  # BGR azul; debe convertirse a RGB.
        return True, frame

    def release(self) -> None:
        self.released.set()


class USBStreamTests(unittest.TestCase):
    def test_recording_waits_for_first_frame_and_finalizes_writer(self) -> None:
        import cv2

        class Writer:
            def __init__(self) -> None:
                self.frames = 0
                self.closed = False

            def isOpened(self) -> bool:
                return True

            def write(self, frame) -> None:
                self.frames += 1

            def release(self) -> None:
                self.closed = True

        capture = _FakeCapture()
        writer = Writer()
        stream = USBCameraStream(capture_factory=lambda _index: capture)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            cv2, "VideoWriter", return_value=writer
        ):
            path = Path(directory) / "test.mp4"
            stream.start(0)
            self.assertEqual(stream.start_recording(path), path)
            self.assertGreaterEqual(writer.frames, 1)
            self.assertTrue(stream.recording)
            result_path, frames = stream.stop_recording()
            self.assertEqual(result_path, path)
            self.assertGreaterEqual(frames, 1)
            self.assertTrue(writer.closed)
            self.assertTrue(stream.stop())

    def test_latest_frame_is_rgb_and_capture_is_released(self) -> None:
        capture = _FakeCapture()
        stream = USBCameraStream(capture_factory=lambda _index: capture)
        stream.start(0)
        frame = None
        deadline = time.monotonic() + 2.0
        while frame is None and time.monotonic() < deadline:
            frame, _status = stream.snapshot()
            time.sleep(0.01)
        self.assertIsNotNone(frame)
        assert frame is not None
        self.assertEqual(frame.shape, (4, 6, 3))
        self.assertEqual(tuple(frame[0, 0]), (0, 0, 255))
        self.assertTrue(stream.stop())
        self.assertTrue(capture.released.is_set())


if __name__ == "__main__":
    unittest.main()
