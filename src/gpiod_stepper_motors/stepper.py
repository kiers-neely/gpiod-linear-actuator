import math
import time
from enum import Enum

import gpiod
from gpiod.line import Direction as LineDirection
from gpiod.line import Value as LineValue


class Direction(Enum):
    FORWARD = True
    REVERSE = False


class StepperMotor:
    """Control a STEP/DIR stepper motor driver using Linux GPIO lines."""

    def __init__(
        self,
        chip: str,
        step_pin: int,
        direction_pin: int,
        *,
        consumer: str = "gpiod-stepper-motors",
        step_interval: float = 0.001,
        pulse_width: float = 0.00001,
        direction_setup_time: float = 0.0,
    ) -> None:
        if not isinstance(chip, str) or not chip:
            raise ValueError("chip must be a non-empty path")
        if (
            type(step_pin) is not int
            or type(direction_pin) is not int
            or step_pin < 0
            or direction_pin < 0
        ):
            raise ValueError("GPIO offsets must be non-negative integers")
        if step_pin == direction_pin:
            raise ValueError("step_pin and direction_pin must be different")
        if not isinstance(consumer, str) or not consumer:
            raise ValueError("consumer must be a non-empty string")
        if (
            not math.isfinite(step_interval)
            or not math.isfinite(pulse_width)
            or not math.isfinite(direction_setup_time)
            or step_interval <= 0
            or pulse_width <= 0
            or pulse_width > step_interval
            or direction_setup_time < 0
        ):
            raise ValueError("invalid step timing")

        self._step_pin = step_pin
        self._direction_pin = direction_pin
        self._step_interval = step_interval
        self._pulse_width = pulse_width
        self._direction_setup_time = direction_setup_time
        self._request = gpiod.request_lines(
            chip,
            consumer=consumer,
            config={
                step_pin: gpiod.LineSettings(
                    direction=LineDirection.OUTPUT,
                    output_value=LineValue.INACTIVE,
                ),
                direction_pin: gpiod.LineSettings(
                    direction=LineDirection.OUTPUT,
                    output_value=LineValue.INACTIVE,
                ),
            },
        )

    def step(self, steps: int, direction: Direction) -> None:
        """Move the motor by emitting the requested number of step pulses."""
        if type(steps) is not int or steps < 0:
            raise ValueError("steps must be a non-negative integer")
        if not isinstance(direction, Direction):
            raise ValueError("direction must be a Direction")
        if self._request is None:
            raise RuntimeError("motor is closed")
        if steps == 0:
            return

        direction_value = (
            LineValue.ACTIVE if direction.value else LineValue.INACTIVE
        )
        self._request.set_value(self._direction_pin, direction_value)
        if self._direction_setup_time:
            time.sleep(self._direction_setup_time)

        try:
            for _ in range(steps):
                self._request.set_value(self._step_pin, LineValue.ACTIVE)
                time.sleep(self._pulse_width)
                self._request.set_value(self._step_pin, LineValue.INACTIVE)
                remaining = self._step_interval - self._pulse_width
                if remaining:
                    time.sleep(remaining)
        finally:
            self._request.set_value(self._step_pin, LineValue.INACTIVE)

    def close(self) -> None:
        """Release the GPIO lines. Safe to call more than once."""
        if self._request is None:
            return

        request = self._request
        self._request = None
        try:
            request.set_value(self._step_pin, LineValue.INACTIVE)
        finally:
            request.release()

    def __enter__(self) -> "StepperMotor":
        if self._request is None:
            raise RuntimeError("motor is closed")
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()
