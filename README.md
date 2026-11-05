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
- **`GPIODevice`** — claims a declarative pin mapping (STEP/DIR/ENA outputs,
  ALARM/limit-switch inputs) as a single `gpiod` line request, and exposes
  `open`/`close`/`get`/`set`/`is_active`. Input lines can also raise a
  synchronous callback when they become active via `on_assert`, dispatched
  through `asyncio`'s `loop.add_reader` with no polling or background
  thread.
- **`GPIOStepperDriver`** — ties a `GPIOConfig` (pin mapping + PWM + ramp
  settings) together with a `GPIODevice`. Still a thin shell; stepping
  logic isn't implemented yet.
- **`load_gpio_config`/`load_pwm_config`/`load_pwm_ramp_config`** — load any
  of this library's pydantic config models from a YAML file.

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

```python
from gpiod_stepper_motors import load_gpio_config
from gpiod_stepper_motors.stepper import GPIODevice
from gpiod_stepper_motors.protocol import GPIOValue, InputLabel, OutputLabel

config = load_gpio_config("examples/gpio_config.yaml")
device = GPIODevice(chip_path=config.chip_path, pin_mapping=config.pin_mapping)

device.open()

# set() accepts a configured label or a raw line offset
device.set(OutputLabel.PUL, GPIOValue.ACTIVE)
device.set(17, GPIOValue.INACTIVE)

# is_active() returns a plain bool; get() returns the raw gpiod line value
if device.is_active(InputLabel.ALARM):
    print("ALARM line is asserted")

raw_value = device.get(InputLabel.MAX_LIMIT)

# on_assert() registers a callback for when an input line becomes active
device.on_assert(InputLabel.ALARM, lambda: print("ALARM asserted!"))
# from inside a running asyncio event loop, start dispatching those
# callbacks via loop.add_reader (no polling, no background thread):
# device._start_reader()

device.close()  # safe to call even if open() was never called, or called twice
```

GPIO offsets, PWM chip/channel numbers, and timing requirements are
hardware- and driver-specific. See `examples/gpio_config.yaml` and
`examples/pwm_config.yaml` for runnable config templates.

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
