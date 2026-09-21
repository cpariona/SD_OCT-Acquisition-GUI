"""Short combined test: USB MP4 pre-roll plus 20 stationary MB frames on NI.

Only run with OCT hardware powered, GUI/NI MAX idle and USB camera available.
The video is decoded and removed from a temporary directory after the test.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import cv2

from octoce.backends.ni_hardware import NIHardwareBackend
from octoce.config import AcquisitionMode, HardwareConfig, ScanParameters, ScanPattern
from octoce.engine import AcquisitionEngine, EngineState
from octoce.usb_camera import USBCameraStream


def main() -> int:
    scan = ScanParameters(
        alines=20, bscans=1, m_repetitions=300, sync_points=50,
        x_length_mm=0.0, y_length_mm=0.0,
        mode=AcquisitionMode.MB, pattern=ScanPattern.LINEAR,
    )
    hardware = HardwareConfig(preview_rate_hz=1.0)
    events = []
    engine = AcquisitionEngine(events.append)
    usb = USBCameraStream()
    with tempfile.TemporaryDirectory(prefix="octoce_combined_") as directory:
        video_path = Path(directory) / "usb_pre_roll.mp4"
        try:
            usb.start(0)
            usb.start_recording(video_path, timeout_s=10.0)
            engine.start(scan, hardware, output_path=None, backend=NIHardwareBackend())
            engine.join(timeout=20.0)
        finally:
            engine.stop()
            engine.join(timeout=5.0)
            _, video_frames = usb.stop_recording()
            usb.stop()
        capture = cv2.VideoCapture(str(video_path))
        decoded = 0
        try:
            while True:
                ok, _ = capture.read()
                if not ok:
                    break
                decoded += 1
        finally:
            capture.release()
        progress = [e.payload for e in events if e.kind == "progress"]
        last = progress[-1] if progress else {}
        healthy = (
            engine.state is EngineState.COMPLETED
            and len(progress) == 20
            and last.get("copied_buffer") == 19
            and last.get("lost_buffers") == 0
            and video_frames > 0
            and decoded == video_frames
        )
        print(
            f"state={engine.state.value} OCT_frames={len(progress)}/20 "
            f"OCT_lost={last.get('lost_buffers')} USB_written={video_frames} "
            f"USB_decoded={decoded} healthy={healthy}"
        )
        return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
