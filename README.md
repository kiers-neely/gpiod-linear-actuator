# gpiod-stepper-motors

A small Python library for controlling STEP/DIR stepper motor drivers through
the Raspberry Pi GPIO character-device interface using the official
[`gpiod`](https://pypi.org/project/gpiod/) bindings for libgpiod. Each
`StepperMotor` instance controls one driver; create one instance per motor.

## Install

This project uses [uv](https://docs.astral.sh/uv/):

```sh
uv sync
```

## Usage

Connect the driver's STEP and DIR inputs to GPIO line offsets on the selected
chip, then use a context manager to release the lines reliably:

```python
from gpiod_stepper_motors import Direction, StepperMotor

with StepperMotor("/dev/gpiochip0", step_pin=17, direction_pin=27) as motor:
    motor.step(200, Direction.FORWARD)
```

The default step interval is 1 ms with a 10 μs pulse. Configure
`step_interval`, `pulse_width`, and `direction_setup_time` as needed for the
driver and motor. GPIO offsets and timing requirements are hardware-specific.