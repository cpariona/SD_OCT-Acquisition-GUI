"""Record a two-second USB video in a temporary directory and verify MP4 decoding.

Run only while the USB camera is not open in another application. No OCT/DAQ
tasks are started. Pass --index N if the preview uses a camera other than 0.
"""

from __future__ import annotations

import argparse
import tempfile
import time
from pathlib import Path

import cv2

from octoce.usb_camera import USBCameraStream


def main(index: int) -> int:
    stream = USBCameraStream()
    with tempfile.TemporaryDirectory(prefix="octoce_usb_test_") as directory:
        path = Path(directory) / "diagnostic.mp4"
        try:
            stream.start(index)
            stream.start_recording(path, timeout_s=10.0)
            time.sleep(2.0)
            _, written = stream.stop_recording()
        finally:
            stream.stop()
        capture = cv2.VideoCapture(str(path))
        decoded = 0
        try:
            while decoded < 200:
                ok, _ = capture.read()
                if not ok:
                    break
                decoded += 1
        finally:
            capture.release()
        healthy = written > 0 and decoded > 0
        print(f"USB_index={index} written_frames={written} decoded_frames={decoded} healthy={healthy}")
        return 0 if healthy else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index", type=int, default=0)
    raise SystemExit(main(parser.parse_args().index))
