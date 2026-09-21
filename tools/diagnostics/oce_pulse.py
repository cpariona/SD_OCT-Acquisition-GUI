"""Explicit PFI13 scope test; does not open the camera or create AO tasks."""
import argparse
from pathlib import Path
from octacq.config import load_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--config", type=Path, default=Path("config/system.toml"))
    parser.add_argument("--period-ms", type=float, required=True, help="Test event interval; HIGH remains fixed")
    parser.add_argument("--pulses", type=int, default=2)
    args = parser.parse_args()
    if not args.execute:
        parser.error("Physical hardware requires --execute")
    h, r = load_config(args.config)
    period, width = args.period_ms * 1e-3, h.oce_pulse_width_us * 1e-6
    if not width < period <= 1 or not 1 <= args.pulses <= 100 or period * args.pulses > 5:
        parser.error("Use a positive LOW interval and at most five seconds of pulses")
    import nidaqmx
    from nidaqmx.constants import AcquisitionType, Level, TaskMode
    with nidaqmx.Task() as task:
        channel = task.co_channels.add_co_pulse_chan_time(f"{h.daq_device}/{h.oce_counter}",
                    idle_state=Level.LOW, initial_delay=period, high_time=width, low_time=period-width)
        channel.co_pulse_term = h.oce_trigger_terminal
        task.timing.cfg_implicit_timing(sample_mode=AcquisitionType.FINITE, samps_per_chan=args.pulses)
        task.control(TaskMode.TASK_COMMIT)
        print(f"terminal={h.oce_trigger_terminal} pulses={args.pulses} "
              f"HIGH_us={channel.co_pulse_high_time*1e6:.6f} LOW_us={channel.co_pulse_low_time*1e6:.6f}")
        task.start()
        task.wait_until_done(timeout=(args.pulses + 1)*period + r.frame_timeout_ms/1000)


if __name__ == "__main__":
    main()
