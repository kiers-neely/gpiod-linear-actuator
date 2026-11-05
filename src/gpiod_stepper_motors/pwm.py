import threading
import time

from loguru import logger
from periphery import PWM, PWMError
from pydantic import BaseModel, NonNegativeFloat, NonNegativeInt, PositiveFloat, PositiveInt


class PWMRampConfig(BaseModel):
    """Linear acceleration/deceleration profile for a `HardwarePWM` channel.

    Describes a single ramp segment: once PWM output is enabled, move from
    the channel's starting frequency to `target_freq_hz`, over
    `ramp_duration_s` seconds, via linear interpolation.

    Attributes:
        target_freq_hz: The frequency, in Hz, to ramp to. Must be greater
            than 0.
        ramp_duration_s: How long the ramp should take, in seconds. Must be
            non-negative; 0 jumps directly to `target_freq_hz`.
    """

    target_freq_hz: PositiveFloat
    ramp_duration_s: NonNegativeFloat


class PWMConfig(BaseModel):
    """Configuration for a single hardware-PWM compatible GPIO pin.

    Describes one PWM channel on a Raspberry Pi (via `python-periphery`)
    and its starting frequency/duty cycle. Optionally embeds a
    `PWMRampConfig`: when set, `HardwarePWM.start_pwm()` ramps up to it
    instead of jumping straight to a fixed frequency.

    Attributes:
        label: A human-readable identifier for this PWM channel, used in
            logs.
        offset: The GPIO line offset this PWM channel is wired to.
        chip: The `python-periphery` PWM chip number.
        channel: The PWM channel number on `chip`.
        frequency_hz: The initial PWM frequency, in Hz.
        duty_cycle: The initial duty cycle, between 0.0 and 1.0.
        ramp_config: Optional ramp settings. `None` means `start_pwm()`
            just enables output at `frequency_hz` with no ramping.
    """

    label: str
    offset: PositiveInt
    chip: NonNegativeInt = 0
    channel: NonNegativeInt = 0
    frequency_hz: float = 1000.0
    duty_cycle: float = 0.0
    ramp_config: PWMRampConfig | None = None


class HardwarePWM:
    """Wraps a single hardware-timed PWM line via `python-periphery`.

    Owns the underlying `periphery.PWM` device for one chip/channel pair,
    and tracks a fault flag that latches whenever a frequency/duty-cycle
    write or an enable/disable call fails. Once faulted, `start_pwm`
    refuses to re-enable output until `_clear_fault` is called.

    `start_pwm`/`stop_pwm` are the only two verbs a caller needs: whether
    output ramps up to `config.ramp_config.target_freq_hz` or jumps
    straight to `config.frequency_hz` is an internal detail driven entirely
    by whether `config.ramp_config` is set, not something the caller has
    to decide between two different objects.

    Args:
        config: The PWM channel's chip/channel, starting frequency, starting
            duty cycle, and optional ramp settings. The device is
            initialized with the frequency/duty cycle immediately on
            construction.

    Attributes:
        config: The `PWMConfig` this instance was constructed with.
        current_freq: The last frequency successfully applied to the
            hardware, kept up to date by every `set_frequency()` call.
    """

    def __init__(self, config: PWMConfig):
        self.config = config
        self._pwm = PWM(chip=config.chip, channel=config.channel)
        self._fault = threading.Event()
        self._fault.clear()
        self.current_freq: float = config.frequency_hz
        self._sweep_thread: threading.Thread | None = None
        self._cancel_event = threading.Event()
        logger.debug("Initializing hardware PWM device...")
        self._initialize_hardware()

    def _initialize_hardware(self):
        self.set_frequency(self.config.frequency_hz)
        self.set_duty_cycle(self.config.duty_cycle)
        logger.debug(f"GPIO{self.config.offset} claimed for hardware-timed PWM.")

    def close_hardware(self):
        """Stop any in-progress ramp and close the underlying PWM device."""
        self._stop_sweep()
        try:
            self._pwm.close()
        except Exception as e:
            logger.error(
                f"Exception caught in closing PWM on channel {self._pwm.channel}: {e}"
            )

    def set_frequency(self, hz: float):
        if hz <= 0:
            raise ValueError("Frequency must be greater than 0")
        try:
            self._pwm.frequency = hz
            self.current_freq = hz
        except PWMError as e:
            self._fault.set()
            logger.error(f"Failed to set frequency: {e}")

    def set_duty_cycle(self, duty: float):
        if not (0.0 <= duty <= 1.0):
            raise ValueError("Duty cycle must be between 0.0 and 1.0")
        try:
            self._pwm.duty_cycle = duty
        except PWMError as e:
            self._fault.set()
            logger.error(f"Failed to set duty cycle: {e}")

    def start_pwm(self) -> None:
        """Enable PWM output.

        If `config.ramp_config` is set, ramps up to its target frequency on
        a background thread after enabling; otherwise output simply runs
        at `current_freq`.
        """
        if self._fault.is_set():
            logger.error(
                "Cannot enable PWM output while faulted. Clear fault and try again"
            )
            return
        try:
            self._pwm.enable()
        except Exception as e:
            self._fault.set()
            raise RuntimeError(
                f"Exception caught in enabling PWM output: {e}. System set to faulted."
            )

        if self.config.ramp_config is not None:
            self._ramp_to(self.config.ramp_config)

    def stop_pwm(self) -> None:
        """Stop any in-progress ramp, then disable PWM output.

        Safe to call regardless of whether a ramp is configured or
        currently running: disabling hardware output directly, without
        stopping the ramp thread first, would leave that thread running
        (with `current_freq` drifting away from reality) until its
        `ramp_duration` elapses. This stops the ramp first, so the two
        stay in sync.
        """
        self._stop_sweep()
        try:
            self._pwm.disable()
        except Exception as e:
            self._fault.set()
            raise RuntimeError(
                f"Exception caught in disabling PWM output: {e}. System set to faulted."
            )

    def _ramp_to(self, ramp_config: PWMRampConfig) -> None:
        # stop any active sweep thread first
        self._stop_sweep()
        if self._sweep_thread and self._sweep_thread.is_alive():
            raise RuntimeError("Previous ramp did not stop in time")
        self._cancel_event.clear()

        # start up a background thread for stepping loop
        self._sweep_thread = threading.Thread(
            target=self._run_ramp,
            args=(ramp_config,),
            daemon=True,
        )
        self._sweep_thread.start()

    def _run_ramp(self, ramp_config: PWMRampConfig) -> None:
        initial_hz = self.current_freq
        target_hz = ramp_config.target_freq_hz
        duration = ramp_config.ramp_duration_s

        delta_hz = target_hz - initial_hz

        # total number of granular steps to make during ramp duration
        # (50-100 updates per sec)
        steps = int(duration * 100)
        if steps == 0:
            self.set_frequency(target_hz)
            return

        time_per_step = duration / steps

        for i in range(1, steps + 1):
            # check if main thread asked to interrupt this move
            if self._cancel_event.is_set():
                break

            # check if PWM hardware reported fault
            if self._is_faulted:
                logger.error("Cancelling move - PWM device reported fault")
                self._cancel_event.set()
                break

            # linear interpolation to calculate step frequency
            progress = i / steps
            next_hz = initial_hz + (delta_hz * progress)

            try:
                self.set_frequency(next_hz)
            except Exception as e:
                logger.error(f"Failed to set step frequency: {e}")

            time.sleep(time_per_step)

    def _stop_sweep(self) -> bool:
        try:
            self._cancel_event.set()
            if self._sweep_thread and self._sweep_thread.is_alive():
                self._sweep_thread.join(timeout=5)
        except Exception as e:
            logger.error(f"Exception caught in stopping PWM ramp: {e}")
            return False
        return True

    def _clear_fault(self) -> bool:
        self._fault.clear()
        return not self._fault.is_set()

    @property
    def _is_faulted(self) -> bool:
        return self._fault.is_set()

    @property
    def _is_enabled(self) -> bool:
        return self._pwm.enabled
