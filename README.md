# gpiod-stepper-motors

A small Python library for driving stepper motors from a Raspberry Pi, using
the official [`gpiod`](https://pypi.org/project/gpiod/) bindings for libgpiod
(digital I/O) and [`python-periphery`](https://pypi.org/project/python-periphery/)
(hardware PWM).

## Install

This project uses [uv](https://docs.astral.sh/uv/):

```sh
uv sync
```

## What's here

- **`HardwarePWM`** — wraps a single hardware-timed PWM channel (frequency,
  duty cycle, enable/disable, fault tracking) via `python-periphery`.
- **`VariableSpeedPWM`** — drives a `HardwarePWM` through a linear frequency
  ramp on a background thread, for accelerating/decelerating a STEP signal
  rather than jumping straight to a target speed.
- **`GPIOStepper`** — declarative STEP/DIR/ENA/ALM/LIM pin mapping via
  `GPIOConfig`, `OutputLine`, and `InputLine`. Not implemented yet.

```python
from gpiod_stepper_motors import HardwarePWM, VariableSpeedPWM
from gpiod_stepper_motors.pwm import PWMConfig

pwm = HardwarePWM(
    PWMConfig(label="step", offset=18, chip=0, channel=0, frequency_hz=200.0)
)
ramp = VariableSpeedPWM(pwm)

pwm.start_pwm()
ramp.ramp_to(2000.0, duration_s=1.5)  # accelerate from 200 Hz to 2000 Hz
```

GPIO offsets, PWM chip/channel numbers, and timing requirements are
hardware- and driver-specific.

## Dev tools

Lint/type-check/test tools (`ruff`, `black`, `mypy`, `pytest`) live in the
`dev` dependency group and are installed by default via `uv sync`.

```sh
uv run pytest
uv run ruff check .
```

`scripts/plot_ramp.py` plots a `VariableSpeedPWM` ramp with matplotlib,
using a mocked PWM device so it runs without any real hardware:

```sh
uv run scripts/plot_ramp.py --start 100 --target 2000 --duration 1.5

# accel -> cruise -> decel profile
uv run scripts/plot_ramp.py --start 100 --peak 2000 --end 100 \
    --accel-duration 1.0 --cruise-duration 0.5 --decel-duration 1.0
```
