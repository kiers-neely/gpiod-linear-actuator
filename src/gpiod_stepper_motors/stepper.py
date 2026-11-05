import asyncio
from collections.abc import Callable
from enum import Enum, StrEnum

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
from .pwm import HardwarePWM, PWMConfig


class LinearActuatorState(StrEnum):
    """Named positions/states for a `LinearActuatorControl` axis.

    `START_MOVE` only accepts `END`/`HOME` as a move target; the other
    members describe status, not a destination you can drive toward.
    """

    END = "END"
    """At (or commanded toward) the MAX_LIMIT end of travel."""
    HOME = "HOME"
    """At (or commanded toward) the MIN_LIMIT end of travel."""
    UNKNOWN = "UNKNOWN"
    """Position not yet established, e.g. before the first homing move."""
    MOVING = "MOVING"
    """A move is currently in progress."""
    ALARM = "ALARM"
    """The driver's ALARM line is latched active."""


class StepperDirection(StrEnum):
    """Raw motion direction for the DIR output line, independent of which
    limit a move is headed toward."""

    FORWARD = "FORWARD"
    REVERSE = "REVERSE"


class StepperEnable(Enum):
    """Logical enable state for the driver's ENA line."""

    ENABLED = True
    DISABLED = False


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
    loaded from YAML via `gpiod_stepper_motors.load_gpio_config`.

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


class LinearActuatorControl:
    """Asynchronous homing-style controller for one linear actuator axis.

    Ties a `GPIOConfig` (pin mapping, PWM channel) to a `GPIOBackend`, and
    drives STEP/DIR/ENA output while reacting to ALARM and limit-switch
    input. `start_move(END | HOME)` drives toward a known end of travel
    until the matching limit switch trips; there's no arbitrary
    distance/step-count move yet, only this homing-style motion.

    Stopping is entirely event-driven: tripping a limit switch or the
    ALARM line calls `.cancel()` on the in-flight move's task via
    `_on_limit`/`_on_alarm`, which is near-instant regardless of
    `start_move`'s `max_duration_s` safety-net timeout. A deliberate
    `stop_move()` call uses that same cancellation path. ALARM is a
    latching fault: once tripped, `clear_alarm()` refuses to clear it
    while the physical line is still asserted, so a human has to resolve
    the fault before motion can resume.

    Typical lifecycle: construct, `await connect()` once (claims GPIO
    lines and the PWM channel, wires up ALARM/limit callbacks), then any
    number of `start_move()`/`stop_move()` calls, finishing with
    `await teardown()`.

    Args:
        config: The pin mapping and PWM channel (with its optional ramp
            settings) for this motor axis.
        gpio: The `GPIOBackend` to drive. Defaults to a real
            `GPIOStepperDriver` built from `config.chip_path`/
            `config.pin_mapping` when omitted; pass one explicitly to
            inject a test double.
    """

    def __init__(self, config: GPIOConfig, gpio: GPIOBackend | None = None):
        self._config = config
        self._name = config.name
        self._gpio = (  # construct real device if a mock has not been injected
            gpio
            if gpio is not None
            else GPIOStepperDriver(
                chip_path=config.chip_path, pin_mapping=config.pin_mapping
            )
        )  # configures the lines only; open resources in connect()
        self._pwm: HardwarePWM | None = None

        self._alarm_active = False
        self._drive_enabled = False
        self._limit_reached: dict[InputLabel, bool] = {}
        self._current_move: asyncio.Task | None = None
        self._connected = False

    @property
    def name(self) -> str:
        """This axis's human-readable identifier, from `config.name`."""
        return self._name

    @property
    def connected(self) -> bool:
        """Whether `connect()` has completed successfully."""
        return self._connected

    @property
    def alarm_active(self) -> bool:
        """Whether the ALARM line is latched active. See `clear_alarm`."""
        return self._alarm_active

    @property
    def drive_enabled(self) -> bool:
        """Whether the driver's ENA line is currently asserted."""
        return self._drive_enabled

    async def connect(self) -> None:
        """
        Claim GPIO lines and the PWM channel, and wire up fault handling.

        Opens the GPIO backend, registers `_on_limit`/`_on_alarm` against
        MAX_LIMIT/MIN_LIMIT/ALARM, starts the edge reader (if the backend
        is a real `GPIOStepperDriver`), constructs the `HardwarePWM`
        channel, and seeds `_limit_reached` from the live hardware state
        so a limit the actuator is already resting against at startup is
        known immediately rather than only after the next edge event.
        Must be called once before `start_move`/`stop_move`.
        """
        logger.debug("Attempting to initialize GPIO device")

        # claim GPIO lines, then assign input callbacks
        self._gpio.open()
        for limit in [InputLabel.MAX_LIMIT, InputLabel.MIN_LIMIT]:
            self._gpio.on_assert(limit, self._on_limit(limit))
        self._gpio.on_assert(InputLabel.ALARM, self._on_alarm)

        # start the edge reader if running on GPIODevice
        if isinstance(self._gpio, GPIOStepperDriver):
            self._gpio._start_reader()

        # claim the pwm channel - ramping is handled internally
        # if ramp_config is set in `PWMConfig`
        self._pwm = HardwarePWM(self._config.pwm)

        # refresh the limit states so the controller knows if
        # which actuator end we're booting up from, if any
        self._limit_reached = await self.refresh_limits()

        self._connected = True

    def _on_alarm(self) -> None:
        """Edge-reader callback for the ALARM line: latch the fault and
        cancel any in-flight move. Cleared only via `clear_alarm`."""
        self._alarm_active = True
        if self._current_move is not None:
            self._current_move.cancel()

    def _on_limit(self, limit: InputLabel) -> Callable[[], None]:
        """Build an edge-reader callback for one limit switch: record that
        `limit` is active and cancel any in-flight move toward it."""

        def handler() -> None:
            self._limit_reached[limit] = True
            if self._current_move is not None:
                self._current_move.cancel()

        return handler

    def enable_drive(self) -> bool:
        """
        Assert the ENA line and verify it took effect.

        Returns:
            bool: True if the line read back as asserted. On a mismatch,
                `drive_enabled` is rolled back to its state before this
                call rather than left inconsistent with the hardware.
        """
        target = GPIOValue.ACTIVE
        prev = self._gpio.get(OutputLabel.ENA)

        self._gpio.set(OutputLabel.ENA, target)

        if self._gpio.get(OutputLabel.ENA) == _GPIO_VALUE[target]:
            self._drive_enabled = True
            return True
        else:
            logger.error("Warning: failed to enable drive")
            self._drive_enabled = bool(prev)
            return False

    def disable_drive(self) -> bool:
        """
        Deassert the ENA line and verify it took effect.

        Returns:
            bool: True if the line read back as deasserted. On a mismatch,
                `drive_enabled` is rolled back to its state before this
                call rather than left inconsistent with the hardware.
        """
        target = GPIOValue.INACTIVE
        prev = self._gpio.get(OutputLabel.ENA)

        self._gpio.set(OutputLabel.ENA, target)

        if self._gpio.get(OutputLabel.ENA) == _GPIO_VALUE[target]:
            self._drive_enabled = False
            return True
        else:
            logger.error("Warning: failed to disable drive")
            self._drive_enabled = bool(prev)
            return False

    async def start_move(
        self, target: LinearActuatorState, max_duration_s: float = 30.0
    ) -> None:
        """
        Drive the actuator toward `target` until the corresponding limit
        switch trips.

        `target` must be END (drives toward MAX_LIMIT) or HOME (drives
        toward MIN_LIMIT) -- this is homing-style motion to a known end of
        travel, not an arbitrary distance/step-count move. Stopping is
        event-driven: `_on_limit`/`_on_alarm` cancel the underlying task
        the instant the relevant line trips; `max_duration_s` is just a
        safety net in case that event never arrives.

        Args:
            target: END or HOME -- which limit to drive toward.
            max_duration_s: Maximum time to allow for the move before
                raising `TimeoutError`, in case the limit switch never
                reports a trip.

        Raises:
            ValueError: If `target` isn't END or HOME.
            RuntimeError: If not connected, the alarm is active, or a move
                is already in progress.
        """
        if target not in (LinearActuatorState.END, LinearActuatorState.HOME):
            raise ValueError(f"start_move target must be END or HOME, got {target}")
        if not self._connected:
            raise RuntimeError("Must call connect() before starting a move")
        if self._alarm_active:
            raise RuntimeError("Cannot move while ALARM is active")
        if self._current_move is not None and not self._current_move.done():
            raise RuntimeError("A move is already in progress")

        limit = (
            InputLabel.MAX_LIMIT
            if target is LinearActuatorState.END
            else InputLabel.MIN_LIMIT
        )
        direction = (
            StepperDirection.FORWARD
            if target is LinearActuatorState.END
            else StepperDirection.REVERSE
        )

        await self.refresh_limits()
        if self._limit_reached.get(limit):
            logger.debug(f"[{self.name}] Already at {target}; nothing to do")
            return

        self._current_move = asyncio.create_task(
            self._run_move(direction, limit, max_duration_s)
        )
        try:
            await self._current_move
        except asyncio.CancelledError:
            logger.debug(f"[{self.name}] Move toward {target} was stopped")
        finally:
            self._current_move = None

    async def _run_move(
        self, direction: StepperDirection, limit: InputLabel, max_duration_s: float = 30.0
    ) -> None:
        """
        Drive STEP/DIR output in `direction` until stopped.

        The primary stop condition is cancellation from `_on_limit`/
        `_on_alarm` (near-instant: the callback calls `.cancel()` on the
        task wrapping this coroutine). `max_duration_s` is a backup only:
        if the limit switch never reports a trip (wiring fault, missed
        event), this stops the actuator on its own rather than running
        indefinitely.

        Raises:
            TimeoutError: If `max_duration_s` elapses with no limit/alarm.
        """
        dir_value = (
            GPIOValue.ACTIVE
            if direction is StepperDirection.FORWARD
            else GPIOValue.INACTIVE
        )

        if not self._drive_enabled:
            self.enable_drive()
            await asyncio.sleep(0.1) # settling time before setting DIR value
        self._gpio.set(OutputLabel.DIR, dir_value)
        self._pwm.start_pwm()

        try:
            # normal stop is cancellation from _on_limit/_on_alarm, which
            # interrupts this sleep immediately; reaching the end of the
            # sleep means neither ever fired, so treat that as a fault
            await asyncio.sleep(max_duration_s)
            raise TimeoutError(
                f"Move toward {limit} did not reach the limit within {max_duration_s}s"
            )
        finally:
            # stop_pwm() can block briefly joining the ramp thread; run it
            # off the event loop so a move-stop never stalls other tasks
            # (including the edge reader) while that join is in progress
            await self._run_blocking(self._pwm.stop_pwm)

    async def stop_move(self) -> bool:
        """
        Cancel any in-flight move and disable PWM output.

        Uses the same cancellation path as `_on_limit`/`_on_alarm`; safe to
        call with no move in progress (it just stops PWM output).

        Returns:
            bool: True if PWM output was successfully stopped, False if
                `stop_pwm()` raised (logged, not re-raised).
        """
        try:
            if self._current_move is not None:
                self._current_move.cancel()
            await self._run_blocking(self._pwm.stop_pwm)
        except Exception as e:
            logger.error(f"Failed to stop motion output: {e}")
            return False
        else:
            return True

    async def _run_blocking(self, func: Callable[[], None]) -> None:
        """Run a synchronous, potentially-blocking callable off the event loop."""
        await asyncio.get_running_loop().run_in_executor(None, func)

    async def refresh_limits(self) -> dict[InputLabel, bool]:
        """
        Polls the max and min limit switches and updates their registry.
        Useful for updating positional state between event handling,
        since `_on_limit` is the only other caller that touches the
        `_limit_reached` dict mapping.

        Returns:
            dict[InputLabel, bool]: Key, value mapping of limit switch
            states, stored True when a limit is reached/active.
        """
        for limit in [InputLabel.MIN_LIMIT, InputLabel.MAX_LIMIT]:
            self._limit_reached[limit] = self._gpio.is_active(limit)
        return self._limit_reached

    def clear_alarm(self) -> bool:
        """
        Clear the latched alarm flag.

        Requires the physical ALARM line to no longer be asserted — this won't
        let you clear a fault that's still physically active. Returns True if
        the flag was cleared, False if the alarm is still asserted.

        Returns:
            bool: True if the alarm flag was cleared, False if it remains active.
        """
        if self._gpio.is_active(InputLabel.ALARM):
            logger.error("Driver alarm is still active; check physical hardware!")
            return False
        self._alarm_active = False
        return True

    async def teardown(self):
        """Stop any move, release the PWM channel, and close GPIO lines.

        Safe to call even if `connect()` never completed."""
        logger.debug(f"[{self.name}] Beginning teardown of stepper driver")

        # call stop to cancel move task and stop any pwm output
        stopped = await self.stop_move()
        if not stopped:
            logger.error("Warning: failed to stop move")

        # release pwm handle
        if self._pwm is not None:
            await self._run_blocking(self._pwm.close_hardware)

        # release gpio lines
        self._gpio.close()
