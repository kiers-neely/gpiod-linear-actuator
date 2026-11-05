import asyncio
from collections.abc import Callable
from enum import Enum, StrEnum

from loguru import logger

from .protocol import GPIOBackend, GPIOValue
from .pwm import HardwarePWM
from .stepper import _GPIO_VALUE, GPIOConfig, GPIOStepperDriver, InputLabel, OutputLabel


class LinearActuatorState(StrEnum):
    """Named positions/states for a `LinearActuatorControl` axis.

    `move_to_target` only accepts `END`/`HOME` as a move target; the other
    members describe status, not a destination you can drive toward.

    Attributes:
        END: At (or commanded toward) the MAX_LIMIT end of travel.
        HOME: At (or commanded toward) the MIN_LIMIT end of travel.
        UNKNOWN: Position not yet established, or stopped mid-move.
        MOVING: In-progress move toward a target state.
        FAULT: A driver's ALARM line latched active.
    """

    END = "END"
    HOME = "HOME"
    UNKNOWN = "UNKNOWN"
    MOVING = "MOVING"
    FAULT = "ALARM"


class StepperDirection(StrEnum):
    """Raw motion direction for the DIR output line, independent of which
    limit a move is headed toward."""

    FORWARD = "FORWARD"
    REVERSE = "REVERSE"


class StepperEnable(Enum):
    """Logical enable state for the driver's ENA line."""

    ENABLED = True
    DISABLED = False


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

        # refresh the limit states so the controller knows
        # which actuator end we're booting up from, if any
        self._limit_reached = await self.refresh_limits()

        self._connected = True

    def _on_alarm(self) -> None:
        """
        Edge-reader callback for the ALARM line.

        Latch the fault and cancel any in-flight move.
        Cleared only via `clear_alarm()`.
        """
        self._alarm_active = True
        if self._current_move is not None:
            self._current_move.cancel()

    def _on_limit(self, limit: InputLabel) -> Callable[[], None]:
        """Build an edge-reader callback for one limit switch.

        Record that `limit` is active and cancel any in-flight move toward it.

        Returns:
            handler (Callable): limit switch callback function.
        """

        def handler() -> None:
            self._limit_reached[limit] = True
            if self._current_move is not None:
                self._current_move.cancel()

        return handler

    async def enable_drive(self) -> bool:
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

    async def disable_drive(self) -> bool:
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

    async def move_to_target(
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
        self,
        direction: StepperDirection,
        limit: InputLabel,
        max_duration_s: float = 30.0,
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
            await self.enable_drive()
            await asyncio.sleep(0.1)  # settling time before setting DIR value
        self._gpio.set(OutputLabel.DIR, dir_value)
        self._pwm.start_pwm()

        try:
            await asyncio.sleep(max_duration_s)
            # normal stop is cancellation from _on_limit/_on_alarm, which
            # interrupts this sleep immediately; reaching the end of the
            # sleep means neither ever fired, so treat that as a fault
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

    async def clear_alarm(self) -> bool:
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
