import math
import threading
import time
from enum import Enum, StrEnum
from typing import Any
from pydantic import BaseModel, NonNegativeInt, PositiveInt
from periphery import PWM, PWMError
from loguru import logger

import gpiod
from gpiod.line import Direction as LineDirection
from gpiod.line import Value as LineValue
from gpiod.line import Edge as LineEdge
from gpiod.line import Bias as LineBias
from pydantic.functional_validators import model_validator
from typing_extensions import Optional


class MotorDirection(Enum):
    FORWARD = True
    REVERSE = False


class Pull(StrEnum):
    UP = "UP"
    DOWN = "DOWN"
    AS_IS = "AS IS"
    DISABLED = "DISABLED"


class GPIOLabel(StrEnum):
    PUL = "PUL"
    DIR = "DIR"
    ENA = "ENA"
    ALM = "ALM"
    LIM = "LIM"


class GPIOValue(Enum):
    ACTIVE = (1, True)
    INACTIVE = (0, False)


_GPIO_PULL = {
    Pull.UP: LineBias.PULL_UP,
    Pull.DOWN: LineBias.PULL_DOWN,
    Pull.AS_IS: LineBias.AS_IS,
    Pull.DISABLED: LineBias.DISABLED,
}

_GPIO_VALUE = {
    GPIOValue.ACTIVE: LineValue.ACTIVE,
    GPIOValue.INACTIVE: LineValue.INACTIVE,
}


class OutputLine(BaseModel):
    """Configuration model for a single GPIO output pin."""

    label: GPIOLabel
    direction: LineDirection = LineDirection.OUTPUT
    offset: PositiveInt
    active_low: bool = False
    initial_value: bool = False


class InputLine(BaseModel):
    """Configuration model for a single GPIO output pin"""

    label: GPIOLabel
    direction: LineDirection = LineDirection.INPUT
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


class GPIOConfig(BaseModel):
    name: str
    chip_path: str = "/dev/gpiochip0"
    pin_mapping: list[InputLine | OutputLine]


class GPIOStepper:
    def __init__(self, config: GPIOConfig) -> None: ...
