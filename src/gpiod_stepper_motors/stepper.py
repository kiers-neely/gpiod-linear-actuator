from enum import StrEnum

from gpiod.chip import Chip
from gpiod.line import Bias as LineBias
from gpiod.line import Direction as LineDirection
from gpiod.line import Edge as LineEdge
from gpiod.line import Value as LineValue
from gpiod.line_request import LineRequest
from gpiod.line_settings import LineSettings
from loguru import logger
from pydantic import BaseModel, PositiveInt

from gpiod_stepper_motors.pwm import PWMConfig, PWMRampConfig

from .protocol import GPIOBackend, GPIOValue, InputLabel, OutputLabel


class Pull(StrEnum):
    """A str enumeration of GPIO pull settings."""

    UP = "UP"
    DOWN = "DOWN"
    AS_IS = "AS IS"
    DISABLED = "DISABLED"


_GPIO_BIAS_FROM_PULL = {
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

    label: OutputLabel
    offset: PositiveInt
    active_low: bool = False
    initial_value: bool = False


class InputLine(BaseModel):
    """Configuration model for a single GPIO output pin."""

    label: InputLabel
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
    """Full declarative configuration for one stepper motor axis.

    Combines a GPIO pin mapping (STEP/DIR/ENA outputs, ALARM/limit-switch
    inputs) with the PWM channel that drives STEP and an optional velocity
    ramp profile. Consumed by `GPIOStepper`; `chip_path` and `pin_mapping`
    are also what `GPIODevice` is built from when no backend is injected.
    Can be loaded from YAML via `gpiod_stepper_motors.load_gpio_config`.

    Attributes:
        name: A human-readable identifier for this axis/motor, used in logs.
        chip_path: Path to the GPIO character device, e.g. "/dev/gpiochip0".
        pin_mapping: The device's configured lines, a mix of `OutputLine`
            (STEP/DIR/ENA-style outputs) and `InputLine` (ALARM/limit-switch
            inputs) entries.
        pwm: Hardware PWM channel configuration driving the STEP signal.
        velocity_profile: Optional acceleration/deceleration ramp settings.
            `None` means the motor runs at a fixed `pwm.frequency_hz` with
            no ramping.

    Example:
        >>> load_gpio_config("examples/gpio_config.yaml")
        GPIOConfig(name='x-axis', chip_path='/dev/gpiochip0', ...)
    """

    name: str
    chip_path: str = "/dev/gpiochip0"
    pin_mapping: list[InputLine | OutputLine]
    pwm: PWMConfig
    velocity_profile: PWMRampConfig | None = None


class GPIODevice(GPIOBackend):
    """Concrete `GPIOBackend` that claims real GPIO lines via `gpiod`.

    Builds a single `gpiod` line-settings mapping from a declarative pin
    mapping (a mix of `OutputLine`s and `InputLine`s) and claims every
    configured line as one combined line request. The `gpiod.Chip` handle
    itself is only opened for the duration of that request (see
    `claim_lines`); the resulting `LineRequest` is independent of the chip
    and is what backs every subsequent `get`/`set`/`is_active` call.

    Args:
        chip_path: Path to the GPIO character device, e.g. "/dev/gpiochip0".
        pin_mapping: The device's configured lines, a mix of `OutputLine`
            (STEP/DIR/ENA-style outputs) and `InputLine` (ALARM/limit-switch
            inputs) entries.

    Attributes:
        line_request: The claimed `gpiod.LineRequest`, or `None` before
            `open()` has been called or after `close()`.

    Example:
        >>> device = GPIODevice(chip_path="/dev/gpiochip0", pin_mapping=pin_mapping)
        >>> device.open()
        >>> device.set(OutputLabel.PUL, GPIOValue.ACTIVE)
        >>> device.is_active(InputLabel.ALARM)
        False
        >>> device.close()
    """

    def __init__(
        self,
        chip_path: str,
        pin_mapping: list[InputLine | OutputLine],
    ):
        self._chip_path = chip_path
        self._offset_by_label = {line.label: line.offset for line in pin_mapping}

        self._line_mappings = {
            line.offset: LineSettings(
                direction=LineDirection.OUTPUT,
                active_low=line.active_low,
                output_value=line.initial_value,
            )
            for line in pin_mapping
            if isinstance(line, OutputLine)
        }

        self._line_mappings.update(
            {
                line.offset: LineSettings(
                    direction=LineDirection.INPUT,
                    edge_detection=line.edge_from_active_logic,
                    bias=_GPIO_BIAS_FROM_PULL[line.pull],
                )
                for line in pin_mapping
                if isinstance(line, InputLine)
            }
        )

        self.line_request = None

    def claim_lines(self) -> LineRequest:
        """
        Claim all of the configured GPIO lines (inputs and outputs) for this
        device in a single request. Open the chip to request the lines only,
        then return the LineRequest to the caller. After the lines are
        claimed, the Chip fd is closed.

        Returns:
            LineRequest: The combined input/output line request object.
        """
        with Chip(self._chip_path) as chip:
            return chip.request_lines(
                config=self._line_mappings, consumer="stepper-driver"
            )

    def open(self):
        """
        Claim every configured GPIO line, storing the resulting LineRequest.

        Raises:
            RuntimeError: If any configured line could not be claimed. The
                original exception is chained via `__cause__`.
        """
        try:
            self.line_request = self.claim_lines()
        except Exception as e:
            logger.error(f"Exception caught in opening GPIO line requests: {e}")
            self.close()
            raise RuntimeError("Failed to claim GPIO lines") from e

    def get(self, line: InputLabel | OutputLabel | int) -> LineValue:
        """
        Polls whether the line is currently asserted (logically active).
        Can look up a line by its configured str label or directly by offset.

        Args:
            line (InputLabel | OutputLabel | int): The label (str) or offset (int)
            value for an `OutputLine` or `InputLine`, assigned at configuration.

        Returns:
            bool: True if the line is actively asserted, False otherwise.
        """

        if isinstance(line, InputLabel | OutputLabel):
            offset = self._offset_by_label.get(line)
            if offset is None:
                raise KeyError(f"Offset for label {line} could not be found")
            return self.line_request.get_value(offset)
        else:
            return self.line_request.get_value(line)

    def set(self, line: InputLabel | OutputLabel | int, value: GPIOValue) -> None:
        """
        Assert or release a single configured GPIO line.
        Can look up a line by its configured str label or directly by offset.

        Args:
            line (InputLabel | OutputLabel | int): The label (str) or offset (int)
                value for an `OutputLine` or `InputLine`, assigned at configuration.
            value (GPIOValue): The logical value to write to the line.

        Raises:
            KeyError: If `line` is a label not found in the configured pin mapping.
        """
        if isinstance(line, InputLabel | OutputLabel):
            offset = self._offset_by_label.get(line)
            if offset is None:
                raise KeyError(f"Offset for label {line} could not be found")
        else:
            offset = line
        self.line_request.set_value(offset, _GPIO_VALUE[value])

    def is_active(self, line: InputLabel | OutputLabel | int) -> bool:
        return self.get(line) == LineValue.ACTIVE

    def close(self):
        """Release the claimed line request, if any. Safe to call twice."""
        if self.line_request is not None:
            logger.debug(
                f"Releasing all lines claimed on {self.line_request.chip_name}"
            )
            self.line_request.release()
            self.line_request = None


class GPIOStepper:
    """Asynchronous GPIO stepper control class."""

    def __init__(self, config: GPIOConfig, gpio: GPIOBackend | None = None):
        self._config = config
        self._gpio = (
            gpio
            if gpio is not None
            else GPIODevice(chip_path=config.chip_path, pin_mapping=config.pin_mapping)
        )
