import math
import threading
import time
from enum import Enum, StrEnum
from typing import Any, Literal
from gpiod_stepper_motors.protocol import GPIOBackend
from gpiod_stepper_motors.pwm import PWMConfig, PWMRampConfig
from pydantic import BaseModel, NonNegativeInt, PositiveInt
from periphery import PWM, PWMError
from loguru import logger

import gpiod
from gpiod.line_settings import LineSettings
from gpiod.line import Direction as LineDirection
from gpiod.line import Value as LineValue
from gpiod.line import Edge as LineEdge
from gpiod.line import Bias as LineBias
from pydantic.functional_validators import model_validator
from typing_extensions import Optional


class Pull(StrEnum):
    """A str enumeration of GPIO pull settings."""

    UP = "UP"
    DOWN = "DOWN"
    AS_IS = "AS IS"
    DISABLED = "DISABLED"


class GPIOLabel(StrEnum):
    """A str enumeration of GPIO pin labels.

    Includes PUL/DIR/ENA for standard stepper motor wiring, plus ALM/LIM for
    optional use with stepper driver ALARM output and LIM for limit switches."""

    PUL = "PUL"
    DIR = "DIR"
    ENA = "ENA"
    ALM = "ALM"
    LIM = "LIM"


_GPIO_BIAS_FROM_PULL = {
    Pull.UP: LineBias.PULL_UP,
    Pull.DOWN: LineBias.PULL_DOWN,
    Pull.AS_IS: LineBias.AS_IS,
    Pull.DISABLED: LineBias.DISABLED,
}


class OutputLine(BaseModel):
    """Configuration model for a single GPIO output pin."""

    label: GPIOLabel
    offset: PositiveInt
    active_low: bool = False
    initial_value: bool = False


class InputLine(BaseModel):
    """Configuration model for a single GPIO output pin."""

    label: GPIOLabel
    offset: PositiveInt
    pull: Pull
    normally_closed: bool = False

    @property
    def active_low(self) -> bool:
        """Determine whether the line is wired active low or high.

        Ensures that physical wiring and line polarity always agree, by using
        the GPIO pin pull (UP/DOWN) and normally open/closed (NO/NC) wiring
        to calculate the `active_low` parameter.

        For standard GPIO button-style input switches, GPIOPull.UP uses the
        Pi's internal resistor to pull the pin HIGH by default (INACTIVE).
        If the corresponding switch is wired NO, the line's active state is
        LOW (0). If wired NC, the line's active state is HIGH (1).

        Returns:
            `True` if the line reads LOW (0) when logically active.
        """
        return self.pull == Pull.UP and not self.normally_closed

    @property
    def edge_from_active_logic(self) -> LineEdge:
        """
        Edge detection for this line, based on active low polarity.

        Returns:
            LineEdge: FALLING if `active_low` is set to True, otherwise
                RISING, following the inactive-to-active edge.
        """
        if self.active_low:
            return LineEdge.FALLING
        else:
            return LineEdge.RISING


class GPIOConfig(BaseModel):
    name: str
    chip_path: str = "/dev/gpiochip0"
    pin_mapping: list[InputLine | OutputLine]
    pwm: PWMConfig
    velocity_profile: Optional[PWMRampConfig] = None


class GPIOStepper:

    def __init__(self, config: GPIOConfig, gpio: GPIOBackend) -> None:
        self.config = config
        self._chip_path = config.chip_path

        self._output_mappings = {
            line.offset: LineSettings(
                direction=LineDirection.OUTPUT,
                active_low=line.active_low,
                output_value=line.initial_value,
            )
            for line in config.pin_mapping
            if isinstance(line, OutputLine)
        }

        self._input_mappings = {
            line.offset: LineSettings(
                direction=LineDirection.INPUT,
                edge_detection=line.edge_from_active_logic,
                bias=_GPIO_BIAS_FROM_PULL[line.pull],
            )
            for line in config.pin_mapping
            if isinstance(line, InputLine)
        }
