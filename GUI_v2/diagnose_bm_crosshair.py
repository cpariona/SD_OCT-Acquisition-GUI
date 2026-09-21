"""Bounded NI BM-crosshair check: 1x1 mm, two X/Y pairs, no saved OCT file.

Only run with OCT hardware powered, GUI/NI MAX idle and the galvo path clear.
PFI12 should produce four frames; PFI13 should pulse twice, on X only.
"""

from __future__ import annotations

import time

import nidaqmx
from nidaqmx.constants import Edge

from octoce.backends.ni_hardware import NIHardwareBackend
from octoce.config import AcquisitionMode, HardwareConfig, ScanParameters, ScanPattern
from octoce.engine import AcquisitionEngine, EngineState


def main() -> int:
    scan = ScanParameters(
        alines=100, bscans=2, m_repetitions=1, sync_points=50,
        x_length_mm=1.0, y_length_mm=1.0,
        mode=AcquisitionMode.BM, pattern=ScanPattern.CROSSHAIR,
    )
    hardware = HardwareConfig(preview_rate_hz=1.0)
    events = []
    engine = AcquisitionEngine(events.append)
    with nidaqmx.Task("octoce_bm_crosshair_edge_readback") as edge_task:
        channel = edge_task.ci_channels.add_ci_count_edges_chan(
            f"{hardware.daq_device}/ctr2", edge=Edge.RISING,
        )
        channel.ci_count_edges_term = hardware.oce_trigger_terminal
        edge_task.start()
        started = time.perf_counter()
        try:
            engine.start(scan, hardware, output_path=None, backend=NIHardwareBackend())
            engine.join(timeout=15.0)
        finally:
            engine.stop()
            engine.join(timeout=5.0)
        elapsed = time.perf_counter() - started
        edges = int(edge_task.read())
    progress = [event.payload for event in events if event.kind == "progress"]
    errors = [event.payload.get("message") for event in events if event.kind == "error"]
    last = progress[-1] if progress else {}
    healthy = (
        engine.state is EngineState.COMPLETED
        and len(progress) == 4
        and edges == 2
        and last.get("copied_buffer") == 3
        and last.get("lost_buffers") == 0
        and not errors
    )
    print(
        f"state={engine.state.value} BM_sweeps={len(progress)}/4 "
        f"PFI13_edges={edges}/2 copied_buffer={last.get('copied_buffer')} "
        f"lost_buffers={last.get('lost_buffers')} elapsed_s={elapsed:.3f} "
        f"healthy={healthy}"
    )
    if errors:
        print(f"errors={errors}")
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
