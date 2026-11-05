# gpiod-linear-actuator

A small Python library for driving stepper motors from a Raspberry Pi, using
the official [`gpiod`](https://pypi.org/project/gpiod/) bindings for libgpiod
(digital I/O) and [`python-periphery`](https://pypi.org/project/python-periphery/)
(hardware PWM).

## Install

This project uses [uv](https://docs.astral.sh/uv/):

```sh
uv sync
```

## Quick start: wiring up a motor

This walks through getting one stepper motor (with a STEP/DIR/ENA-style
driver, an ALARM output, and two limit switches) running end to end.

### 1. Wire it up

Each signal you connect gets a label from `InputLabel`/`OutputLabel` and a
GPIO line offset on your board. A typical setup looks like:

| Signal                                  | Direction | Label                   |
|------------------------------------------|-----------|--------------------------|
| STEP pulse input on your driver          | output    | `OutputLabel.PUL`       |
| DIR input on your driver                 | output    | `OutputLabel.DIR`       |
| ENA (enable) input on your driver        | output    | `OutputLabel.ENA`       |
| ALARM output from your driver            | input     | `InputLabel.ALARM`      |
| Limit switch at the far end of travel    | input     | `InputLabel.MAX_LIMIT`  |
| Limit switch at the home end of travel   | input     | `InputLabel.MIN_LIMIT`  |

ENA isn't required by every driver, and not every setup has both limit
switches wired, but ALARM and at least one limit switch are what
`LinearActuatorControl` needs to do anything useful.

### 2. Describe your wiring in a config file

Copy `examples/gpio_config.yaml` to something like `my_motor.yaml` and edit
the `offset` values to match the GPIO lines you actually wired (use
`gpioinfo` to find them). Also adjust `pwm.frequency_hz`/`duty_cycle` and
`pwm.ramp_config` for your motor's speed and acceleration:

```yaml
name: x-axis
chip_path: /dev/gpiochip0

pin_mapping:
  - label: PUL
    offset: 17
  - label: DIR
    offset: 27
  - label: ENA
    offset: 22
  - label: ALARM
    offset: 23
    pull: UP
    normally_closed: false
  - label: MAX_LIMIT
    offset: 24
    pull: UP
    normally_closed: true
  - label: MIN_LIMIT
    offset: 25
    pull: UP
    normally_closed: true

pwm:
  label: step
  offset: 17
  chip: 0
  channel: 0
  frequency_hz: 200.0
  duty_cycle: 0.5
  ramp_config:
    target_freq_hz: 2000.0
    ramp_duration_s: 1.5
```

`pull`/`normally_closed` describe how each input switch is wired, not just
a polarity guess: `pull: UP` means the line reads HIGH (inactive) until
pulled LOW, and `normally_closed` says whether the switch's resting state
is closed or open. Between the two, the library works out the correct
active-high/active-low logic for you (see `InputLine.active_low`).

### 3. Write your control script

This is real code for actual hardware — no mocking, nothing simulated:

```python
import asyncio

from gpiod_linear_actuator import load_gpio_config
from gpiod_linear_actuator.stepper import LinearActuatorControl, LinearActuatorState


async def main():
    config = load_gpio_config("my_motor.yaml")
    actuator = LinearActuatorControl(config)

    await actuator.connect()  # claims GPIO lines + the PWM channel
    try:
        await actuator.start_move(LinearActuatorState.HOME)
        print("Homed.")

        await actuator.start_move(LinearActuatorState.END)
        print("At far end of travel.")
    finally:
        await actuator.teardown()  # always release lines/PWM, even on error


asyncio.run(main())
```

`start_move` drives toward the given limit and returns once that limit
switch trips — there's no separate "wait for the move to finish" step.

### 4. Handle faults

If the ALARM line asserts mid-move, `start_move` returns immediately
(the move is cancelled, not timed out) and `actuator.alarm_active` becomes
`True`. Every subsequent `start_move` call refuses until the alarm is
cleared — and `clear_alarm()` itself refuses unless the physical ALARM
line has already gone back to inactive, so a person has to actually fix
the fault first:

```python
if actuator.alarm_active:
    if actuator.clear_alarm():
        print("Alarm cleared — safe to move again")
    else:
        print("ALARM line is still asserted; check the driver and wiring")
```

## What's here

- **`HardwarePWM`** — wraps a single hardware-timed PWM channel (frequency,
  duty cycle, enable/disable, fault tracking) via `python-periphery`.
  `start_pwm()`/`stop_pwm()` are the only two verbs a caller needs: if
  `PWMConfig.ramp_config` is set, `start_pwm()` ramps up to it on a
  background thread instead of jumping straight to a fixed frequency, and
  `stop_pwm()` always stops that ramp (if any) before disabling output, so
  there's a single source of truth regardless of whether ramping is
  configured.
- **`GPIOStepperDriver`** — claims a declarative pin mapping (STEP/DIR/ENA
  outputs, ALARM/limit-switch inputs) as a single `gpiod` line request, and
  exposes `open`/`close`/`get`/`set`/`is_active`. Input lines can also raise
  a synchronous callback when they become active via `on_assert`,
  dispatched through `asyncio`'s `loop.add_reader` with no polling or
  background thread.
- **`LinearActuatorControl`** — ties a `GPIOConfig` (pin mapping + PWM
  channel) to a `GPIOBackend` and drives homing-style motion: `connect()`
  claims hardware and wires up ALARM/limit-switch handling, then
  `start_move(END | HOME)` drives toward a limit until it trips.
  Tripping a limit switch or the ALARM line cancels an in-flight move
  immediately via the same `on_assert` callbacks; ALARM latches until
  `clear_alarm()` is called *and* the physical line has already gone
  inactive, so a human has to resolve the fault before motion can resume.
- **`load_gpio_config`/`load_pwm_config`/`load_pwm_ramp_config`** — load any
  of this library's pydantic config models from a YAML file.

### Lower-level building blocks

`LinearActuatorControl` is built out of `HardwarePWM` and
`GPIOStepperDriver` — reach for these directly if you want PWM-only output,
or raw GPIO access without the homing/fault-handling behavior above.

```python
from gpiod_linear_actuator import HardwarePWM
from gpiod_linear_actuator.pwm import PWMConfig, PWMRampConfig

pwm = HardwarePWM(
    PWMConfig(
        label="step",
        offset=18,
        chip=0,
        channel=0,
        frequency_hz=200.0,
        ramp_config=PWMRampConfig(target_freq_hz=2000.0, ramp_duration_s=1.5),
    )
)

pwm.start_pwm()  # enables output, then ramps 200 Hz -> 2000 Hz in the background
...
pwm.stop_pwm()  # stops the ramp (if still running) and disables output together
```

```python
from gpiod_linear_actuator import load_gpio_config
from gpiod_linear_actuator.stepper import GPIOStepperDriver
from gpiod_linear_actuator.protocol import GPIOValue, InputLabel, OutputLabel

config = load_gpio_config("examples/gpio_config.yaml")
device = GPIOStepperDriver(chip_path=config.chip_path, pin_mapping=config.pin_mapping)

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

`scripts/plot_ramp.py` plots a `HardwarePWM` ramp with matplotlib, using a
mocked PWM device so it runs without any real hardware:

```sh
uv run scripts/plot_ramp.py --start 100 --target 2000 --duration 1.5

# accel -> cruise -> decel profile
uv run scripts/plot_ramp.py --start 100 --peak 2000 --end 100 \
    --accel-duration 1.0 --cruise-duration 0.5 --decel-duration 1.0
```

## Try it without hardware

The "Quick start" script above needs a real GPIO chip and PWM hardware to
actually run. To see `LinearActuatorControl`'s full behavior — homing
moves, an ALARM trip cancelling a move mid-flight, the refusal to move
again until the fault is cleared — without any of that, this swaps in an
in-memory `GPIOBackend` stand-in (a plain mock). That's the same seam
`GPIOBackend` is designed for, so this runs as-is, no GPIO chip attached:

```python
import asyncio
from unittest.mock import MagicMock, patch

from gpiod.line import Value
from gpiod_linear_actuator import load_gpio_config
from gpiod_linear_actuator.protocol import InputLabel
from gpiod_linear_actuator.stepper import LinearActuatorControl, LinearActuatorState


async def main():
    with patch("gpiod_linear_actuator.pwm.PWM"):  # no real PWM hardware needed
        config = load_gpio_config("examples/gpio_config.yaml")

        fake_gpio = MagicMock()
        fake_gpio.is_active.return_value = False
        fake_gpio.get.return_value = Value.ACTIVE  # ENA readback for enable_drive()

        actuator = LinearActuatorControl(config, gpio=fake_gpio)
        await actuator.connect()  # claims lines/PWM, wires up ALARM + limit handling

        # normally the limit switch trips on its own via the edge reader;
        # here we fire the same callback directly to simulate it
        async def simulate_limit_trip():
            await asyncio.sleep(0.05)
            actuator._on_limit(InputLabel.MAX_LIMIT)()

        asyncio.create_task(simulate_limit_trip())
        await actuator.start_move(LinearActuatorState.END, max_duration_s=5.0)
        print(f"Reached END, alarm_active={actuator.alarm_active}")

        # an ALARM trip cancels an in-flight move immediately, the same way
        async def simulate_alarm_trip():
            await asyncio.sleep(0.05)
            actuator._on_alarm()

        asyncio.create_task(simulate_alarm_trip())
        await actuator.start_move(LinearActuatorState.HOME, max_duration_s=5.0)
        print(f"Move interrupted, alarm_active={actuator.alarm_active}")

        try:
            await actuator.start_move(LinearActuatorState.HOME)
        except RuntimeError as e:
            print(f"Refused: {e}")  # alarm must be cleared first

        fake_gpio.is_active.return_value = False  # physical ALARM line now clear
        print(f"clear_alarm() -> {actuator.clear_alarm()}")

        await actuator.teardown()


asyncio.run(main())
```

Against real hardware, drop the `gpio=fake_gpio` argument entirely —
`LinearActuatorControl(config)` builds a real `GPIOStepperDriver` from
`config.chip_path`/`config.pin_mapping`, and a tripped limit switch or
ALARM line calls `_on_limit`/`_on_alarm` the same way, via the edge reader
instead of a direct call.
