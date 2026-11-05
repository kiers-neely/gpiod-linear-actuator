from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel

from gpiod_linear_actuator.pwm import PWMConfig, PWMRampConfig
from gpiod_linear_actuator.stepper import GPIOConfig

ConfigT = TypeVar("ConfigT", bound=BaseModel)


def load_yaml_config(path: str | Path, model: type[ConfigT]) -> ConfigT:
    """Load a YAML file and validate it against a pydantic config model."""
    with open(path) as f:
        data = yaml.safe_load(f)
    return model.model_validate(data or {})


def load_gpio_config(path: str | Path) -> GPIOConfig:
    return load_yaml_config(path, GPIOConfig)


def load_pwm_config(path: str | Path) -> PWMConfig:
    return load_yaml_config(path, PWMConfig)


def load_pwm_ramp_config(path: str | Path) -> PWMRampConfig:
    return load_yaml_config(path, PWMRampConfig)
