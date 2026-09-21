"""Bounded, no-file check of hardware-clocked stationary MB alignment.

Run only when the OCT GUI and NI MAX are not acquiring.  The script centres
AO0/AO1, enables PFI12/PFI13, acquires a bounded number of blocks (100 by
default), then stops and parks.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import nidaqmx
import numpy as np
from nidaqmx.constants import Edge
from PIL import Image

from octoce.backends.ni_hardware import NIHardwareBackend
from octoce.config import (
    AcquisitionMode, HardwareConfig, ScanParameters, ScanPattern, stationary_alignment_timing,
)
from octoce.engine import AcquisitionEngine, EngineEvent, EngineState
from octoce.processing import normalize_preview


def main(blocks: int = 100) -> int:
    if not 1 <= blocks <= 1000:
        raise ValueError("La prueba debe usar entre 1 y 1000 bloques.")
    scan = ScanParameters(
        alines=1,
        bscans=1,
        m_repetitions=1000,
        sync_points=0,
        x_length_mm=0.0,
        y_length_mm=0.0,
        mode=AcquisitionMode.MB,
        pattern=ScanPattern.LINEAR,
    )
    hardware, block_rate_hz = stationary_alignment_timing(HardwareConfig(), 1000)
    events: list[EngineEvent] = []
    progress_times: list[float] = []
    preview_ranges: list[tuple[float, float]] = []
    preview_images: list[np.ndarray] = []

    def on_event(event: EngineEvent) -> None:
        events.append(event)
        if event.kind == "progress":
            progress_times.append(time.perf_counter())
            if event.payload["acquired_alines"] >= blocks * 1000:
                engine.stop()
        elif event.kind == "preview":
            db = event.payload["intensity_db"]
            preview_ranges.append((float(np.nanmin(db)), float(np.nanmax(db))))
            preview_images[:] = [np.array(db, copy=True)]

    engine = AcquisitionEngine(on_event)
    engine.set_preview_remove_dc(False)
    engine.set_preview_depth_range(1, 1024)
    with nidaqmx.Task("octoce_alignment_scope_readback") as edge_task:
        edge_channel = edge_task.ci_channels.add_ci_count_edges_chan(
            f"{hardware.daq_device}/ctr2", edge=Edge.RISING
        )
        edge_channel.ci_count_edges_term = hardware.oce_trigger_terminal
        edge_task.start()
        started = time.perf_counter()
        try:
            engine.start(
                scan,
                hardware,
                output_path=None,
                backend=NIHardwareBackend(
                    continuous_alignment=True, alignment_block_rate_hz=block_rate_hz
                ),
                continuous=True,
            )
            engine.join(timeout=max(12.0, blocks / block_rate_hz + 10.0))
        finally:
            engine.stop()
            engine.join(timeout=5.0)
        elapsed = time.perf_counter() - started
        edges = int(edge_task.read())

    errors = [event.payload["message"] for event in events if event.kind == "error"]
    progress = [event.payload for event in events if event.kind == "progress"]
    intervals = np.diff(progress_times)
    print(
        f"state={engine.state.value} blocks={len(progress)} PFI13_rising_edges={edges} "
        f"camera_klps={hardware.effective_line_rate_hz/1000:.3f} trigger_hz={block_rate_hz:.3f}"
    )
    print(f"whole_test_s={elapsed:.4f}")
    if intervals.size:
        print(
            f"buffer_intervals_ms median={np.median(intervals)*1000:.3f} "
            f"p10={np.percentile(intervals,10)*1000:.3f} p90={np.percentile(intervals,90)*1000:.3f} "
            f"rate_hz={1/np.mean(intervals):.3f}"
        )
    if progress:
        last = progress[-1]
        print(
            f"acquired_alines={last['acquired_alines']} lost_buffers={last['lost_buffers']} "
            f"last_copied_buffer={last['copied_buffer']} throughput_mib_s={last['throughput_mib_s']:.2f}"
        )
    print(f"previews={len(preview_ranges)} preview_db_range={preview_ranges[-1] if preview_ranges else None}")
    if preview_images:
        preview_path = Path(__file__).parent / "data" / "alignment_50hz_preview.png"
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        Image.fromarray(normalize_preview(preview_images[0])).save(preview_path)
        print(f"preview_png={preview_path}")
    if errors:
        print(f"errors={errors}")
    measured_rate_hz = 1 / float(np.mean(intervals)) if intervals.size else 0.0
    healthy = (
        engine.state is EngineState.STOPPED
        and len(progress) == blocks
        and 49.0 <= measured_rate_hz <= 51.0
        and len(progress) <= edges <= len(progress) + 2
        and progress[-1]["lost_buffers"] == 0
        and progress[-1]["copied_buffer"] == blocks - 1
        and bool(preview_ranges)
        and not errors
    )
    print(f"healthy={healthy}")
    return 0 if healthy else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blocks", type=int, default=100)
    raise SystemExit(main(parser.parse_args().blocks))
