"""Explicit physical test: consecutive buffers across two acquisitions in one session.

AO moves along the requested pattern; PFI12 is active and PFI13 is optional.
Use an oscilloscope to check physical timing. Host durations are not gap measurements.
"""
import argparse
from pathlib import Path
from threading import Event
import time
from octacq.config import ScanRequest, load_config
from octacq.scan import plan
from octacq.hardware.system import HardwareSystem


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Explicitly enable physical hardware")
    parser.add_argument("--config", type=Path, default=Path("config/system.toml"))
    parser.add_argument("--pattern", choices=("stationary", "crosshair"), default="stationary")
    parser.add_argument("--oce", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        parser.error("Physical hardware requires --execute")
    hardware, runtime = load_config(args.config)
    stationary = args.pattern == "stationary"
    request = ScanRequest(pattern=args.pattern, mode="MB" if stationary else "BM",
                          alines=4 if stationary else 100, bscans=2,
                          m_repetitions=100 if stationary else 1,
                          x_length_mm=0 if stationary else 1,
                          y_length_mm=0 if stationary else 1,
                          center_x_mm=hardware.park_x_mm, center_y_mm=hardware.park_y_mm,
                          oce_enabled=args.oce)
    schedule = plan(request, hardware)
    system = HardwareSystem(hardware, runtime)
    try:
        system.connect(request.frame_lines)
        session = system.camera.session_id.value
        last = -1
        for run in range(2):
            first = last + 1
            count = 0
            started = time.monotonic()
            for frame, number, raw in system.execute(schedule, Event()):
                if number != last + 1:
                    raise RuntimeError("Nonconsecutive buffer")
                last = number
                count += len(raw)
            if count != request.expected_alines or system.camera.session_id.value != session:
                raise RuntimeError("Wrong payload count or session was replaced")
            print(f"run={run + 1} session={session} buffers={first}..{last} alines={count} "
                  f"host_duration_s={time.monotonic()-started:.6f} statistics={system.statistics}")
            if run == 0:
                time.sleep(3)  # intentional idle-session test, outside acquisition
        print(f"Scope expectation per run: PFI12={request.frame_count}, "
              f"PFI13={sum(f.oce for f in schedule.frames())}, "
              f"PFI13 HIGH={hardware.oce_pulse_width_us if args.oce else 0} us, "
              f"sweep_period_s={schedule.period_s:.9f}")
    finally:
        system.disconnect()


if __name__ == "__main__":
    main()
