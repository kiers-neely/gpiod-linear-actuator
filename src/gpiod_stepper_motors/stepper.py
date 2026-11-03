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


class GPIOPull(StrEnum):
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
    GPIOPull.UP: LineBias.PULL_UP,
    GPIOPull.DOWN: LineBias.PULL_DOWN,
    GPIOPull.AS_IS: LineBias.AS_IS,
    GPIOPull.DISABLED: LineBias.DISABLED,
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
    pull: GPIOPull
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
        return self.pull == GPIOPull.UP and not self.normally_closed


class PWMConfig(BaseModel):
    """Configuration for a single hardware-PWM compatible GPIO pin on a Raspberry Pi."""

    label: str
    offset: PositiveInt
    chip: NonNegativeInt = 0
    channel: NonNegativeInt = 0
    frequency_hz: float = 1000.0
    duty_cycle: float = 0.0


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

    def __init__(self, pwm_device: HardwarePWM):
        self.pwm = pwm_device
        self.current_freq: float = pwm_device.config.frequency_hz
        self._sweep_thread: threading.Thread | None = None
        self._cancel_event = threading.Event()

    def ramp_to(self, target_freq: float, duration_s: float):
        if target_freq <= 0:
            raise ValueError("target_freq must be greater than 0")
        if duration_s < 0:
            raise ValueError("duration_s must be non-negative")

        # stop any active sweep thread first
        self.stop_sweep()
        if self._sweep_thread and self._sweep_thread.is_alive():
            raise RuntimeError("Previous ramp did not stop in time")
        self._cancel_event.clear()

        # start up a background thread for stepping loop
        self._sweep_thread = threading.Thread(
            target=self._run_ramp,
            args=(target_freq, duration_s),
            daemon=True,
        )
        self._sweep_thread.start()

    def _run_ramp(self, target: float, duration: float):
        initial_hz = self.current_freq
        delta_hz = target - initial_hz

        # total number of granular steps to make during ramp duration
        # (50-100 updates per sec)
        steps = int(duration * 100)
        if steps == 0:
            self.pwm.set_frequency(target)
            self.current_freq = target
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


class GPIOConfig(BaseModel):
    name: str
    chip_path: str = "/dev/gpiochip0"
    pin_mapping: list[InputLine | OutputLine]


class GPIOStepper:
    def __init__(self, config: GPIOConfig) -> None: ...
