import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from gpiod_stepper_motors.config import (
    load_gpio_config,
    load_pwm_config,
    load_pwm_ramp_config,
    load_yaml_config,
)
from gpiod_stepper_motors.pwm import PWMConfig, PWMRampConfig
from gpiod_stepper_motors.stepper import GPIOConfig, InputLine, OutputLine

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"


class YamlConfigLoaderTests(unittest.TestCase):
    def write_yaml(self, text: str) -> Path:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as tmp:
            tmp.write(text)
            tmp.close()
            self.addCleanup(lambda: Path(tmp.name).unlink(missing_ok=True))
            return Path(tmp.name)

    def test_load_pwm_config_example_file(self):
        config = load_pwm_config(EXAMPLES_DIR / "pwm_config.yaml")

        self.assertIsInstance(config, PWMConfig)
        self.assertEqual(config.label, "step")
        self.assertEqual(config.offset, 18)
        self.assertEqual(config.frequency_hz, 200.0)
        self.assertIsInstance(config.ramp_config, PWMRampConfig)
        assert config.ramp_config is not None
        self.assertEqual(config.ramp_config.target_freq_hz, 2000.0)

    def test_load_gpio_config_example_file(self):
        config = load_gpio_config(EXAMPLES_DIR / "gpio_config.yaml")

        self.assertIsInstance(config, GPIOConfig)
        self.assertEqual(config.name, "x-axis")
        self.assertEqual(len(config.pin_mapping), 6)
        self.assertIsInstance(config.pin_mapping[0], OutputLine)
        self.assertIsInstance(config.pin_mapping[-1], InputLine)
        self.assertEqual(config.pwm.label, "step")
        assert config.velocity_profile is not None
        self.assertEqual(config.velocity_profile.ramp_duration_s, 1.5)

    def test_load_pwm_ramp_config_from_yaml(self):
        path = self.write_yaml("target_freq_hz: 500.0\nramp_duration_s: 0.75\n")

        config = load_pwm_ramp_config(path)

        self.assertEqual(
            config, PWMRampConfig(target_freq_hz=500.0, ramp_duration_s=0.75)
        )

    def test_load_yaml_config_raises_on_missing_required_field(self):
        path = self.write_yaml("label: step\n")  # missing required 'offset'

        with self.assertRaises(ValidationError):
            load_yaml_config(path, PWMConfig)

    def test_load_yaml_config_raises_on_empty_file(self):
        path = self.write_yaml("")

        with self.assertRaises(ValidationError):
            load_yaml_config(path, PWMConfig)


if __name__ == "__main__":
    unittest.main()
