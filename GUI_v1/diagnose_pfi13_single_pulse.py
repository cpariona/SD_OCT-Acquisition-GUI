"""Compare two finite PFI13 counter configurations using PFI input readback.

The test emits at most two 10 us pulses on PFI13, with AO0/AO1 fixed at 0 V.
Run only while the OCT GUI and NI MAX are not acquiring.
"""

from __future__ import annotations

import time
from contextlib import ExitStack
from threading import Event

import nidaqmx
import numpy as np
from nidaqmx.constants import AcquisitionType, Edge, Level, TaskMode
from nidaqmx.stream_writers import AnalogMultiChannelWriter

from octoce.backends.ni_daq import NIDaqGalvoController
from octoce.config import AcquisitionMode, HardwareConfig, ScanParameters, ScanPattern
from octoce.scan import ScanPlanner


def count_one_pulse(*, implicit: bool) -> int:
    with ExitStack() as stack:
        counter_input = stack.enter_context(nidaqmx.Task("octoce_pfi13_edge_readback"))
        counter_output = stack.enter_context(nidaqmx.Task("octoce_pfi13_single_pulse"))
        ao = stack.enter_context(nidaqmx.Task("octoce_pfi13_ao_master"))

        edge_channel = counter_input.ci_channels.add_ci_count_edges_chan(
            "Dev1/ctr2", edge=Edge.RISING
        )
        edge_channel.ci_count_edges_term = "/Dev1/PFI13"

        pulse_channel = counter_output.co_channels.add_co_pulse_chan_time(
            "Dev1/ctr1",
            idle_state=Level.LOW,
            initial_delay=0.001,
            low_time=90e-6,
            high_time=10e-6,
        )
        pulse_channel.co_pulse_term = "/Dev1/PFI13"
        if implicit:
            counter_output.timing.cfg_implicit_timing(
                sample_mode=AcquisitionType.FINITE, samps_per_chan=1
            )
        counter_output.triggers.start_trigger.cfg_dig_edge_start_trig(
            "/Dev1/ao/StartTrigger", trigger_edge=Edge.RISING
        )

        ao.ao_channels.add_ao_voltage_chan("Dev1/ao0:1", min_val=-10.0, max_val=10.0)
        ao.timing.cfg_samp_clk_timing(
            rate=50_000.0,
            sample_mode=AcquisitionType.FINITE,
            samps_per_chan=200,
        )
        writer = AnalogMultiChannelWriter(ao.out_stream, auto_start=False)
        writer.write_many_sample(np.zeros((2, 200), dtype=np.float64), timeout=5.0)

        counter_input.control(TaskMode.TASK_COMMIT)
        counter_output.control(TaskMode.TASK_COMMIT)
        ao.control(TaskMode.TASK_COMMIT)
        counter_input.start()
        counter_output.start()
        ao.start()
        counter_output.wait_until_done(timeout=2.0)
        ao.wait_until_done(timeout=2.0)
        time.sleep(0.01)
        return int(counter_input.read())


def count_alignment_segments(count: int = 3) -> int:
    """Count PFI13 edges in the real application task group."""
    hardware = HardwareConfig()
    scan = ScanParameters(
        alines=1,
        bscans=1,
        m_repetitions=1000,
        sync_points=50,
        x_length_mm=0.0,
        y_length_mm=0.0,
        mode=AcquisitionMode.MB,
        pattern=ScanPattern.LINEAR,
    )
    segment = next(ScanPlanner(scan, hardware).iter_segments())
    controller = NIDaqGalvoController(hardware)
    with nidaqmx.Task("octoce_pfi13_alignment_readback") as counter_input:
        edge_channel = counter_input.ci_channels.add_ci_count_edges_chan(
            "Dev1/ctr2", edge=Edge.RISING
        )
        edge_channel.ci_count_edges_term = "/Dev1/PFI13"
        counter_input.start()
        try:
            for _ in range(count):
                controller.start_segment(segment)
                controller.wait_segment(Event(), timeout_s=2.0)
            return int(counter_input.read())
        finally:
            controller.park()


if __name__ == "__main__":
    for name, implicit in (("implicit finite, 1 pulse", True), ("single pulse, no implicit", False)):
        edges = count_one_pulse(implicit=implicit)
        print(f"{name}: {edges} flanco(s) medido(s) en PFI13")
    edges = count_alignment_segments()
    print(f"3 grupos MB de la aplicacion: {edges} flanco(s) medido(s) en PFI13")
