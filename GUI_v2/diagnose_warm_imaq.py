"""Two short MB acquisitions in one process to verify warm NI-IMAQ reuse.

No galvo sweep or OCT file. Requires powered/idle NI hardware. The cached
session is explicitly released when this script finishes.
"""

from __future__ import annotations

import time

from octoce.backends.ni_hardware import NIHardwareBackend
from octoce.config import AcquisitionMode, HardwareConfig, ScanParameters
from octoce.engine import AcquisitionEngine, EngineState


def main() -> int:
    scan = ScanParameters(
        alines=10, bscans=1, m_repetitions=300, sync_points=50,
        x_length_mm=0.0, y_length_mm=0.0, mode=AcquisitionMode.MB,
    )
    hardware = HardwareConfig(preview_rate_hz=1.0)
    results = []
    try:
        for trial in (1, 2):
            events = []
            engine = AcquisitionEngine(events.append)
            started = time.perf_counter()
            try:
                engine.start(
                    scan, hardware, output_path=None,
                    backend=NIHardwareBackend(warm_camera=True),
                )
                engine.join(timeout=15.0)
            finally:
                engine.stop()
                engine.join(timeout=5.0)
            elapsed = time.perf_counter() - started
            progress = [event.payload for event in events if event.kind == "progress"]
            last = progress[-1] if progress else {}
            terminal = next(
                (event.payload for event in reversed(events)
                 if event.kind == "state" and event.payload.get("state") == "completed"),
                {},
            )
            result = (
                engine.state is EngineState.COMPLETED and len(progress) == 10
                and last.get("lost_buffers") == 0
            )
            results.append((elapsed, result))
            print(
                f"trial={trial} state={engine.state.value} frames={len(progress)} "
                f"last_buffer={last.get('copied_buffer')} lost={last.get('lost_buffers')} "
                f"total_s={elapsed:.3f} active_s={terminal.get('elapsed_s')} healthy={result}"
            )
    finally:
        NIHardwareBackend.release_warm_camera()
    return 0 if all(ok for _, ok in results) and results[1][0] < results[0][0] else 1


if __name__ == "__main__":
    raise SystemExit(main())
