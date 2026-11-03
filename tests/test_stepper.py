import unittest
from unittest.mock import patch

from gpiod.line import Value

from gpiod_stepper_motors import MotorDirection, StepperMotor
from gpiod_stepper_motors.stepper import HardwarePWM, PWMConfig


class StepperMotorTests(unittest.TestCase):
    def setUp(self):
        self.request_patcher = patch("gpiod_stepper_motors.stepper.gpiod.request_lines")
        self.request_lines = self.request_patcher.start()
        self.addCleanup(self.request_patcher.stop)
        self.request = self.request_lines.return_value
        self.sleep_patcher = patch("gpiod_stepper_motors.stepper.time.sleep")
        self.sleep = self.sleep_patcher.start()
        self.addCleanup(self.sleep_patcher.stop)

    def make_motor(self, **kwargs):
        return StepperMotor("/dev/gpiochip0", 17, 27, **kwargs)

    def test_requests_step_and_direction_outputs_in_inactive_state(self):
        self.make_motor()

        self.request_lines.assert_called_once()
        config = self.request_lines.call_args.kwargs["config"]
        self.assertEqual(config[17].output_value, Value.INACTIVE)
        self.assertEqual(config[27].output_value, Value.INACTIVE)

    def test_step_emits_pulses_in_requested_direction(self):
        motor = self.make_motor(step_interval=0.001, pulse_width=0.00001)

        motor.step(2, MotorDirection.FORWARD)

        self.assertEqual(
            self.request.set_value.call_args_list,
            [
                unittest.mock.call(27, Value.ACTIVE),
                unittest.mock.call(17, Value.ACTIVE),
                unittest.mock.call(17, Value.INACTIVE),
                unittest.mock.call(17, Value.ACTIVE),
                unittest.mock.call(17, Value.INACTIVE),
                unittest.mock.call(17, Value.INACTIVE),
            ],
        )
        self.assertEqual(self.sleep.call_count, 4)

    def test_context_manager_releases_gpio_lines(self):
        with self.make_motor() as motor:
            motor.step(1, MotorDirection.REVERSE)

        self.request.release.assert_called_once()
        with self.assertRaises(RuntimeError):
            motor.step(1, MotorDirection.FORWARD)

    def test_rejects_invalid_pin_and_timing(self):
        with self.assertRaises(ValueError):
            StepperMotor("/dev/gpiochip0", 17, 17)
        with self.assertRaises(ValueError):
            self.make_motor(step_interval=0.00001, pulse_width=0.001)
        self.request_lines.assert_not_called()


class HardwarePWMTests(unittest.TestCase):
    def setUp(self):
        self.pwm_patcher = patch("gpiod_stepper_motors.stepper.PWM")
        self.pwm_cls = self.pwm_patcher.start()
        self.addCleanup(self.pwm_patcher.stop)
        self.pwm = self.pwm_cls.return_value

    def make_pwm(self, **kwargs):
        config = PWMConfig(label="test", offset=12, **kwargs)
        return HardwarePWM(config)

    def test_initializes_hardware_with_configured_chip_and_channel(self):
        self.make_pwm(chip=2, channel=1, frequency_hz=500.0, duty_cycle=0.25)

        self.pwm_cls.assert_called_once_with(chip=2, channel=1)
        self.assertEqual(self.pwm.frequency, 500.0)
        self.assertEqual(self.pwm.duty_cycle, 0.25)

    def test_starts_unfaulted(self):
        pwm = self.make_pwm()

        self.assertFalse(pwm._is_faulted)

    def test_set_frequency_rejects_non_positive_values(self):
        pwm = self.make_pwm()

        with self.assertRaises(ValueError):
            pwm.set_frequency(0)
        with self.assertRaises(ValueError):
            pwm.set_frequency(-10)

    def test_set_duty_cycle_rejects_out_of_range_values(self):
        pwm = self.make_pwm()

        with self.assertRaises(ValueError):
            pwm.set_duty_cycle(-0.1)
        with self.assertRaises(ValueError):
            pwm.set_duty_cycle(1.1)

    def test_start_pwm_enables_output_when_not_faulted(self):
        pwm = self.make_pwm()

        pwm.start_pwm()

        self.pwm.enable.assert_called_once()

    def test_start_pwm_refuses_when_faulted(self):
        pwm = self.make_pwm()
        pwm._fault.set()

        pwm.start_pwm()

        self.pwm.enable.assert_not_called()

    def test_start_pwm_sets_fault_when_enable_raises(self):
        pwm = self.make_pwm()
        self.pwm.enable.side_effect = RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            pwm.start_pwm()

        self.assertTrue(pwm._is_faulted)

    def test_stop_pwm_sets_fault_when_disable_raises(self):
        pwm = self.make_pwm()
        self.pwm.disable.side_effect = RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            pwm.stop_pwm()

        self.assertTrue(pwm._is_faulted)

    def test_clear_fault_resets_faulted_state(self):
        pwm = self.make_pwm()
        pwm._fault.set()

        result = pwm._clear_fault()

        self.assertTrue(result)
        self.assertFalse(pwm._is_faulted)

    def test_is_enabled_reflects_underlying_pwm_state(self):
        pwm = self.make_pwm()
        self.pwm.enabled = True

        self.assertTrue(pwm._is_enabled)

    def test_close_hardware_closes_underlying_pwm(self):
        pwm = self.make_pwm()

        pwm.close_hardware()

        self.pwm.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
