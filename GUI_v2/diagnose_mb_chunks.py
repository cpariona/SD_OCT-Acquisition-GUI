"""Small NI hardware check for finite, optimized MB chunks (no galvo sweep).

Run only after powering the OCT hardware and closing any acquiring GUI/NI MAX
task.  AO0/AO1 remain at 0 V.  The default emits 10 PFI12/PFI13 pulses and
captures 10 buffers of 300 raw A-lines each; no .bin file is written.
"""

from __future__ import annotations

import argparse
import time

import nidaqmx
from nidaqmx.constants import Edge

from octoce.backends.ni_hardware import NIHardwareBackend
from octoce.config import AcquisitionMode, HardwareConfig, ScanParameters, ScanPattern
from octoce.engine import AcquisitionEngine, EngineEvent, EngineState


def main(alines: int = 10) -> int:
    if not 1 <= alines <= 128:
        raise ValueError("Use entre 1 y 128 posiciones para esta prueba acotada.")
    scan = ScanParameters(
        alines=alines,
        bscans=1,
        m_repetitions=300,
        sync_points=50,
        x_length_mm=0.0,
        y_length_mm=0.0,
        mode=AcquisitionMode.MB,
        pattern=ScanPattern.LINEAR,
    )
    hardware = HardwareConfig(preview_rate_hz=1.0)
    events: list[EngineEvent] = []
    engine = AcquisitionEngine(events.append)
    with nidaqmx.Task("octoce_mb_chunk_edge_readback") as edge_task:
        edge_channel = edge_task.ci_channels.add_ci_count_edges_chan(
            f"{hardware.daq_device}/ctr2", edge=Edge.RISING
        )
        edge_channel.ci_count_edges_term = hardware.oce_trigger_terminal
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
    errors = [event.payload["message"] for event in events if event.kind == "error"]
    last = progress[-1] if progress else {}
    terminal = next(
        (event.payload for event in reversed(events)
         if event.kind == "state" and event.payload.get("state") in
         {"completed", "stopped", "error"}),
        {},
    )
    active_s = terminal.get("elapsed_s")
    healthy = (
        engine.state is EngineState.COMPLETED
        and len(progress) == alines
        and edges == alines
        and last.get("copied_buffer") == alines - 1
        and last.get("lost_buffers") == 0
        and not errors
    )
    timing = f"elapsed_s={elapsed:.3f}"
    if active_s is not None:
        timing += f" active_s={float(active_s):.3f} startup_shutdown_s={elapsed - float(active_s):.3f}"
    print(
        f"state={engine.state.value} MB_positions={len(progress)}/{alines} "
        f"PFI13_edges={edges} copied_buffer={last.get('copied_buffer')} "
        f"lost_buffers={last.get('lost_buffers')} {timing} healthy={healthy}"
    )
    if errors:
        print(f"errors={errors}")
    return 0 if healthy else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alines", type=int, default=10)
    raise SystemExit(main(parser.parse_args().alines))
