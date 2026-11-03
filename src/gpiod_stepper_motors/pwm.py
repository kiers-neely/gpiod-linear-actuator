import threading
import time
from typing import Optional

from loguru import logger
from periphery import PWM, PWMError
from pydantic import BaseModel, NonNegativeInt, PositiveInt


class PWMRampConfig(BaseModel):
    target_freq_hz: float
    ramp_duration_s: float


class PWMConfig(BaseModel):
    """Configuration for a single hardware-PWM compatible GPIO pin on a Raspberry Pi."""

    label: str
    offset: PositiveInt
    chip: NonNegativeInt = 0
    channel: NonNegativeInt = 0
    frequency_hz: float = 1000.0
    duty_cycle: float = 0.0
    ramp_config: Optional[PWMRampConfig] = None


class HardwarePWM:
    """Wrapper for handling a single hardware-timed PWM line using python-periphery."""

    def __init__(self, config: PWMConfig):
        self.config = config
        self._pwm = PWM(chip=config.chip, channel=config.channel)
        self._fault = threading.Event()
        self._fault.clear()
        logger.debug("Initializing hardware PWM device...")
        self._initialize_hardware()

    def _initialize_hardware(self):
        self.set_frequency(self.config.frequency_hz)
        self.set_duty_cycle(self.config.duty_cycle)
        logger.debug(f"GPIO{self.config.offset} claimed for hardware-timed PWM.")

    def close_hardware(self):
        try:
            self._pwm.close()
        except Exception as e:
            logger.error(
                f"Exception caught in closing PWM on channel {self._pwm.channel}"
            )

    def set_frequency(self, hz: float):
        if hz <= 0:
            raise ValueError("Frequency must be greater than 0")
        try:
            self._pwm.frequency = hz
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

    def start_pwm(self):
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

    def stop_pwm(self):
        try:
            self._pwm.disable()
        except Exception as e:
            self._fault.set()
            raise RuntimeError(
                f"Exception caught in disabling PWM output: {e}. System set to faulted."
            )

    def _clear_fault(self) -> bool:
        self._fault.clear()
        if self._fault.is_set():
            return False
        return True

    @property
    def _is_faulted(self) -> bool:
        return self._fault.is_set()

    @property
    def _is_enabled(self) -> bool:
        return self._pwm.enabled


class VariableSpeedPWM:
    """Drives a HardwarePWM through a linear frequency ramp on a background thread."""

    def __init__(self, pwm_device: HardwarePWM, ramp_config: PWMRampConfig):
        self.pwm = pwm_device
        self.config = ramp_config
        self.target_freq = ramp_config.target_freq_hz
        self.ramp_duration = ramp_config.ramp_duration_s

        self.current_freq: float = pwm_device.config.frequency_hz
        self._sweep_thread: threading.Thread | None = None
        self._cancel_event = threading.Event()

    def ramp_to(self):
        if self.target_freq <= 0:
            raise ValueError("target_freq must be greater than 0")
        if self.ramp_duration < 0:
            raise ValueError("duration_s must be non-negative")

        # stop any active sweep thread first
        self.stop_sweep()
        if self._sweep_thread and self._sweep_thread.is_alive():
            raise RuntimeError("Previous ramp did not stop in time")
        self._cancel_event.clear()

        # start up a background thread for stepping loop
        self._sweep_thread = threading.Thread(
            target=self._run_ramp,
            daemon=True,
        )
        self._sweep_thread.start()

    def _run_ramp(self):
        initial_hz = self.current_freq
        target_hz = self.target_freq
        duration = self.ramp_duration

        delta_hz = target_hz - initial_hz

        # total number of granular steps to make during ramp duration
        # (50-100 updates per sec)
        steps = int(duration * 100)
        if steps == 0:
            self.pwm.set_frequency(target_hz)
            self.current_freq = target_hz
            return

        time_per_step = duration / steps

        for i in range(1, steps + 1):
            # check if main thread asked to interrupt this move
            if self._cancel_event.is_set():
                break

            # check if PWM hardware reported fault
            if self.pwm._is_faulted:
                logger.error("Cancelling move - PWM device reported fault")
                self._cancel_event.set()
                break

            # linear interpolation to calculate step frequency
            progress = i / steps
            next_hz = initial_hz + (delta_hz * progress)

            try:
                self.pwm.set_frequency(next_hz)
                self.current_freq = next_hz
            except Exception as e:
                logger.error(f"Failed to set step frequency: {e}")

            time.sleep(time_per_step)

    def stop_sweep(self) -> bool:
        try:
            self._cancel_event.set()
            if self._sweep_thread and self._sweep_thread.is_alive():
                self._sweep_thread.join(timeout=5)
        except Exception as e:
            logger.error(f"Exception caught in stopping PWM ramp: {e}")
            return False
        return True
