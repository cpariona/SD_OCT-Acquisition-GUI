"""Short, standalone PFI13 scope check. Does not move galvos or open the camera."""

from __future__ import annotations

import argparse
import time

from octoce.config import HardwareConfig, OCE_TRIGGER_DUTY_CYCLE


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pulses", type=int, default=1000)
    parser.add_argument("--width-us", type=float, default=HardwareConfig().oce_pulse_width_us)
    parser.add_argument("--arm-delay-s", type=float, default=2.0)
    args = parser.parse_args()
    period_us = args.width_us / OCE_TRIGGER_DUTY_CYCLE
    if not 2 <= args.pulses <= 10_000:
        parser.error("--pulses debe estar entre 2 y 10000.")
    if not 1.0 <= args.width_us <= 100.0:
        parser.error("--width-us debe estar entre 1 y 100 us.")
    if args.pulses * period_us > 1_000_000:
        parser.error("La emisión se limita a un segundo; reduzca --pulses o --width-us.")
    if not 0.0 <= args.arm_delay_s <= 10.0:
        parser.error("--arm-delay-s debe estar entre 0 y 10 s.")

    import nidaqmx
    from nidaqmx.constants import AcquisitionType, Level, TaskMode

    cfg = HardwareConfig()
    with nidaqmx.Task("octoce_pfi13_scope_check") as task:
        channel = task.co_channels.add_co_pulse_chan_time(
            f"{cfg.daq_device}/{cfg.oce_counter}",
            idle_state=Level.LOW,
            initial_delay=0.001,
            high_time=args.width_us * 1e-6,
            low_time=(period_us - args.width_us) * 1e-6,
        )
        channel.co_pulse_term = cfg.oce_trigger_terminal
        task.timing.cfg_implicit_timing(
            sample_mode=AcquisitionType.FINITE,
            samps_per_chan=args.pulses,
        )
        task.control(TaskMode.TASK_COMMIT)
        high_us = channel.co_pulse_high_time * 1e6
        low_us = channel.co_pulse_low_time * 1e6
        actual_duty = high_us / (high_us + low_us)
        print(
            f"{cfg.oce_trigger_terminal}: {args.pulses} pulsos; "
            f"HIGH={high_us:.3f} us, LOW={low_us:.3f} us, "
            f"duty={actual_duty:.2%}; inicio en {args.arm_delay_s:g} s."
        )
        print("Osciloscopio: entrada de 1 Mohm, referencia D GND, captura por flanco ascendente.")
        time.sleep(args.arm_delay_s)
        task.start()
        task.wait_until_done(timeout=max(2.0, args.pulses * period_us * 1e-6 + 1.0))
    print("Emision completada. La tension fisica debe verificarse en el osciloscopio.")


if __name__ == "__main__":
    main()
