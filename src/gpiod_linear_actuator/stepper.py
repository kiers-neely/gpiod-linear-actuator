import asyncio
from collections.abc import Callable
from enum import StrEnum

from gpiod.chip import Chip
from gpiod.edge_event import EdgeEvent
from gpiod.line import Bias as LineBias
from gpiod.line import Direction as LineDirection
from gpiod.line import Edge as LineEdge
from gpiod.line import Value as LineValue
from gpiod.line_request import LineRequest
from gpiod.line_settings import LineSettings
from loguru import logger
from pydantic import BaseModel, PositiveInt

from .protocol import GPIOBackend, GPIOValue, InputLabel, OutputLabel
from .pwm import PWMConfig


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
    """Configuration model for a single GPIO input pin."""

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
    def active_edge(self) -> LineEdge:
        """
        The `gpiod` edge-detection setting for this line becoming active.

        Used to configure `LineSettings.edge_detection` so the kernel only
        reports the inactive-to-active transition; every edge event read
        for this line is therefore an assertion by construction.

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
    inputs) with the PWM channel that drives STEP. Consumed by
    `LinearActuatorControl`; `chip_path` and `pin_mapping` are also what
    `GPIOStepperDriver` is built from when no backend is injected. Can be
    loaded from YAML via `gpiod_linear_actuator.load_gpio_config`.

    Attributes:
        name: A human-readable identifier for this axis/motor, used in logs.
        chip_path: Path to the GPIO character device, e.g. "/dev/gpiochip0".
        pin_mapping: The device's configured lines, a mix of `OutputLine`
            (STEP/DIR/ENA-style outputs) and `InputLine` (ALARM/limit-switch
            inputs) entries.
        pwm: Hardware PWM channel configuration driving the STEP signal.
            Set `pwm.ramp_config` to have the motor accelerate/decelerate
            instead of running at a fixed `pwm.frequency_hz`.

    Example:
        >>> load_gpio_config("examples/gpio_config.yaml")
        GPIOConfig(name='x-axis', chip_path='/dev/gpiochip0', ...)
    """

    name: str
    chip_path: str = "/dev/gpiochip0"
    pin_mapping: list[InputLine | OutputLine]
    pwm: PWMConfig


class GPIOStepperDriver(GPIOBackend):
    """Concrete `GPIOBackend` that claims real GPIO lines via `gpiod`.

    Builds a single `gpiod` line-settings mapping from a declarative pin
    mapping (a mix of `OutputLine`s and `InputLine`s) and claims every
    configured line as one combined line request. The `gpiod.Chip` handle
    itself is only opened for the duration of that request (see
    `claim_lines`); the resulting `LineRequest` is independent of the chip
    and is what backs every subsequent `get`/`set`/`is_active` call.

    Input lines can also raise a synchronous callback when they become
    active (a limit switch tripping, an alarm asserting): register one
    with `on_assert`, then call `_start_reader()` from inside a running
    asyncio event loop. The reader watches the line request's file
    descriptor via `loop.add_reader` and dispatches matching callbacks as
    events arrive, with no polling and no background thread. `close()`
    always stops the reader before releasing the claimed lines.

    Args:
        chip_path: Path to the GPIO character device, e.g. "/dev/gpiochip0".
        pin_mapping: The device's configured lines, a mix of `OutputLine`
            (STEP/DIR/ENA-style outputs) and `InputLine` (ALARM/limit-switch
            inputs) entries.

    Attributes:
        line_request: The claimed `gpiod.LineRequest`, or `None` before
            `open()` has been called or after `close()`.

    Example:
        >>> device = GPIOStepperDriver(chip_path="/dev/gpiochip0", pin_mapping=pin_mapping)
        >>> device.open()
        >>> device.set(OutputLabel.PUL, GPIOValue.ACTIVE)
        >>> device.is_active(InputLabel.ALARM)
        False
        >>> device.on_assert(InputLabel.ALARM, lambda: print("ALARM asserted!"))
        >>> device.close()
    """

    def __init__(
        self,
        chip_path: str,
        pin_mapping: list[InputLine | OutputLine],
    ):
        self._chip_path = chip_path

        # create line lookup table by str label, for ease of access
        self._line_by_label = {line.label: line for line in pin_mapping}
        self._line_by_offset = {line.offset: line for line in pin_mapping}

        # store both input and output lines in one mapping, so all lines can be
        # claimed in a single request
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
                    edge_detection=line.active_edge,
                    bias=_GPIO_BIAS_FROM_PULL[line.pull],
                    active_low=line.active_low,
                )
                for line in pin_mapping
                if isinstance(line, InputLine)
            }
        )

        # store callbacks in cohesive registry
        self._callbacks_by_offset: dict[int, Callable[[], None]] = {}

        self.line_request = None
        self._loop: asyncio.AbstractEventLoop | None = None

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

    def close(self):
        """
        Shuts down the edge reader and closes the line request, if any.

        Reader must be stopped before releasing claimed lines.
        """
        try:
            self._stop_reader()
        except Exception as e:
            logger.error(f"Exception caught in shutting down edge reader: {e}")

        if self.line_request is not None:
            logger.debug(
                f"Releasing all lines claimed on {self.line_request.chip_name}"
            )
            self.line_request.release()
        self.line_request = None

    def is_active(self, line: InputLabel | OutputLabel | int) -> bool:
        """Returns whether a GPIO line is currently active."""
        return self.get(line) == LineValue.ACTIVE

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
            configured_line = self._line_by_label.get(line)
            if configured_line is None:
                raise KeyError(f"No offset found for {line} label")
            offset = configured_line.offset
        else:
            offset = line

        return self.line_request.get_value(offset)

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
            configured_line = self._line_by_label.get(line)
            if configured_line is None:
                raise KeyError(f"No offset found for {line} label")
            offset = configured_line.offset
        else:
            offset = line

        self.line_request.set_value(offset, _GPIO_VALUE[value])

    def on_assert(self, line: InputLabel | int, callback: Callable[[], None]) -> None:
        """
        Store a callback to be called each time a given GPIO line is activated.

        Args:
            line (InputLabel | int): The str label or offset of the input line.
            callback (Callable[[], None]): Called on active edge event.

        Raises:
            KeyError: If `line` is a label not found in the configured
            pin mapping.
        """
        if isinstance(line, InputLabel):
            configured_line = self._line_by_label.get(line)
            if configured_line is None:
                raise KeyError(f"No offset found for {line} label")
            offset = configured_line.offset
        else:
            offset = line
        self._callbacks_by_offset[offset] = callback

    def _handle_edge_event(self, event: EdgeEvent) -> None:
        """Dispatch a single edge event to its registered callback.

        No-ops if no callback for this event is configured.
        """
        callback = self._callbacks_by_offset.get(event.line_offset)
        if callback is not None:
            try:
                callback()
            except Exception as e:
                logger.error(f"Exception raised in on_assert callback: {e}")

    def _monitor_fd(self) -> None:
        """
        Drain all pending edge events and dispatch each one.

        Registered with the event loop by `_start_reader`.
        """
        for event in self.line_request.read_edge_events():
            self._handle_edge_event(event)

    def _start_reader(self) -> None:
        """
        Start dispatching `on_assert` callbacks via the running event loop.

        Must be called from inside a running asyncio event loop, after
        lines have been claimed (see `open`). Registers `_monitor_fd` with
        `loop.add_reader` on the claimed line request's file descriptor.

        Raises:
            RuntimeError: If called before lines have been claimed.
        """
        if self.line_request is None:
            raise RuntimeError("Cannot start reader before claiming lines")
        self._loop = asyncio.get_running_loop()
        self._loop.add_reader(self.line_request.fd, self._monitor_fd)

    def _stop_reader(self) -> None:
        """Stop dispatching `on_assert` callbacks.

        Safe to call before `_start_reader()` has ever run, or more than
        once.
        """
        if self._loop is not None and self.line_request is not None:
            self._loop.remove_reader(self.line_request.fd)
        self._loop = None
