"""Hardware check for bidirectional linear BM in horizontal and vertical axes.

Runs two small 1 mm scans, M=2, without writing files. Close the GUI and NI MAX
before running. The planner and automated tests verify forward/return AO order;
this check verifies camera buffers and the expected PFI13 event count.
"""

from __future__ import annotations

import nidaqmx
from nidaqmx.constants import Edge

from octoce.backends.ni_hardware import NIHardwareBackend
from octoce.config import AcquisitionMode, HardwareConfig, Orientation, ScanParameters, ScanPattern
from octoce.engine import AcquisitionEngine, EngineState


def main() -> int:
    hardware = HardwareConfig(preview_rate_hz=1.0)
    results = []
    with nidaqmx.Task("octoce_linear_bidirectional_edges") as edge_task:
        channel = edge_task.ci_channels.add_ci_count_edges_chan(
            f"{hardware.daq_device}/ctr2", edge=Edge.RISING,
        )
        channel.ci_count_edges_term = hardware.oce_trigger_terminal
        edge_task.start()
        try:
            for orientation in (Orientation.HORIZONTAL, Orientation.VERTICAL):
                scan = ScanParameters(
                    alines=100, bscans=1, m_repetitions=2, sync_points=50,
                    x_length_mm=1.0, y_length_mm=1.0,
                    mode=AcquisitionMode.BM, pattern=ScanPattern.LINEAR,
                    orientation=orientation,
                )
                events = []
                engine = AcquisitionEngine(events.append)
                try:
                    engine.start(
                        scan, hardware, output_path=None,
                        backend=NIHardwareBackend(warm_camera=True),
                    )
                    engine.join(timeout=15.0)
                finally:
                    engine.stop()
                    engine.join(timeout=5.0)
                progress = [event.payload for event in events if event.kind == "progress"]
                last = progress[-1] if progress else {}
                healthy = (
                    engine.state is EngineState.COMPLETED and len(progress) == 2
                    and last.get("lost_buffers") == 0
                )
                results.append(healthy)
                print(
                    f"axis={orientation.value} state={engine.state.value} sweeps={len(progress)}/2 "
                    f"last_buffer={last.get('copied_buffer')} lost={last.get('lost_buffers')} "
                    f"healthy={healthy}"
                )
            edges = int(edge_task.read())
        finally:
            NIHardwareBackend.release_warm_camera()
    print(f"PFI13_edges={edges}/4")
    return 0 if all(results) and edges == 4 else 1


if __name__ == "__main__":
    raise SystemExit(main())
