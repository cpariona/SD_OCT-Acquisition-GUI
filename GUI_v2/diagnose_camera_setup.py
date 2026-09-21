"""Time NI-IMAQ/camera setup steps without enabling DAQ counters or AO.

Only run when the GUI and NI MAX are idle. The camera is opened/configured and
closed; no PFI12/PFI13 pulses or galvo sweeps are generated.
"""

from __future__ import annotations

import time
from unittest.mock import patch

from octoce.backends.ni_hardware import NIHardwareBackend
from octoce.backends.ni_imaq import NIIMAQCamera
from octoce.config import AcquisitionMode, HardwareConfig, ScanParameters


class TimedCamera(NIIMAQCamera):
    events: list[tuple[str, float]] = []

    def __init__(self):
        super().__init__()
        dll = self._dll
        events = self.events

        class TimedDLL:
            def __getattr__(self, name):
                original = getattr(dll, name)

                def wrapped(*args):
                    started = time.perf_counter()
                    try:
                        return original(*args)
                    finally:
                        events.append((name, time.perf_counter() - started))

                return wrapped

        self._dll = TimedDLL()

    def _timed(self, label, call, *args):
        started = time.perf_counter()
        try:
            return call(*args)
        finally:
            self.events.append((label, time.perf_counter() - started))

    def set_camera_attribute(self, name, value):
        return self._timed(f"set {name}", super().set_camera_attribute, name, value)

    def set_camera_attribute_numeric(self, name, value):
        return self._timed(f"set {name}", super().set_camera_attribute_numeric, name, value)

    def get_camera_attribute(self, name):
        return self._timed(f"get {name}", super().get_camera_attribute, name)

    def get_camera_attribute_numeric(self, name):
        return self._timed(f"get {name}", super().get_camera_attribute_numeric, name)


def main() -> None:
    scan = ScanParameters(
        alines=10, bscans=1, m_repetitions=300, sync_points=50,
        x_length_mm=0.0, y_length_mm=0.0, mode=AcquisitionMode.MB,
    )
    hardware = HardwareConfig()
    backend = NIHardwareBackend()
    with patch("octoce.backends.ni_hardware.NIIMAQCamera", TimedCamera):
        started = time.perf_counter()
        try:
            backend.open(scan, hardware)
            opened = time.perf_counter()
        finally:
            backend.close()
        closed = time.perf_counter()
    for label, duration in TimedCamera.events:
        print(f"{label}: {duration:.3f} s")
    print(f"open_total={opened - started:.3f} s close_total={closed - opened:.3f} s")


if __name__ == "__main__":
    main()
