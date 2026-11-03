"""Plot a VariableSpeedPWM ramp without any real GPIO/PWM hardware.

Usage:
    uv run scripts/plot_ramp.py --start 100 --target 2000 --duration 1.5

    # accel -> cruise -> decel profile
    uv run scripts/plot_ramp.py --start 100 --peak 2000 --end 100 \\
        --accel-duration 1.0 --cruise-duration 0.5 --decel-duration 1.0
"""

import argparse
import time
from unittest.mock import patch

from gpiod_stepper_motors.stepper import HardwarePWM, PWMConfig, VariableSpeedPWM


def record_ramp(start_hz: float, segments: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Run a sequence of (target_hz, duration_s) ramps back-to-back and record (time, hz) samples."""
    samples: list[tuple[float, float]] = []

    with patch("gpiod_stepper_motors.stepper.PWM"):
        config = PWMConfig(label="plot", offset=1, frequency_hz=start_hz)
        pwm = HardwarePWM(config)
        ramp = VariableSpeedPWM(pwm)

        start_time = time.monotonic()
        original_set_frequency = pwm.set_frequency

        def recording_set_frequency(hz: float) -> None:
            original_set_frequency(hz)
            samples.append((time.monotonic() - start_time, hz))

        pwm.set_frequency = recording_set_frequency
        samples.append((0.0, start_hz))

        for target_hz, duration_s in segments:
            if duration_s <= 0:
                continue
            ramp.ramp_to(target_hz, duration_s)
            ramp._sweep_thread.join(timeout=duration_s + 5)

    return samples


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", type=float, default=100.0, help="Starting frequency in Hz")
    parser.add_argument("--target", type=float, default=None, help="Target frequency for a single ramp (Hz)")
    parser.add_argument("--duration", type=float, default=1.0, help="Duration of a single ramp, in seconds")
    parser.add_argument("--peak", type=float, default=None, help="Peak frequency for an accel/decel profile (Hz)")
    parser.add_argument("--end", type=float, default=None, help="Final frequency after decel (Hz, defaults to --start)")
    parser.add_argument("--accel-duration", type=float, default=1.0, help="Time to ramp from --start to --peak")
    parser.add_argument("--cruise-duration", type=float, default=0.0, help="Time to hold at --peak before decelerating")
    parser.add_argument("--decel-duration", type=float, default=1.0, help="Time to ramp from --peak to --end")
    parser.add_argument("--output", type=str, default=None, help="Save plot to this path instead of showing it")
    args = parser.parse_args()

    if args.peak is not None:
        end_hz = args.start if args.end is None else args.end
        segments = [
            (args.peak, args.accel_duration),
            (args.peak, args.cruise_duration),
            (end_hz, args.decel_duration),
        ]
        title = (
            f"VariableSpeedPWM accel/decel: {args.start} -> {args.peak} -> {end_hz} Hz"
        )
    else:
        target_hz = 1000.0 if args.target is None else args.target
        segments = [(target_hz, args.duration)]
        title = f"VariableSpeedPWM ramp: {args.start} Hz -> {target_hz} Hz over {args.duration}s"

    samples = record_ramp(args.start, segments)
    times, freqs = zip(*samples)

    import matplotlib.pyplot as plt

    plt.plot(times, freqs, marker=".")
    plt.xlabel("Time (s)")
    plt.ylabel("Frequency (Hz)")
    plt.title(title)
    plt.grid(True)

    if args.output:
        plt.savefig(args.output)
        print(f"Saved plot to {args.output}")
    else:
        plt.show()


if __name__ == "__main__":
    main()
