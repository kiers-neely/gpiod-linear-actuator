import unittest
from unittest.mock import patch

from gpiod.line import Value

from gpiod_stepper_motors import Direction, StepperMotor


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

        motor.step(2, Direction.FORWARD)

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
            motor.step(1, Direction.REVERSE)

        self.request.release.assert_called_once()
        with self.assertRaises(RuntimeError):
            motor.step(1, Direction.FORWARD)

    def test_rejects_invalid_pin_and_timing(self):
        with self.assertRaises(ValueError):
            StepperMotor("/dev/gpiochip0", 17, 17)
        with self.assertRaises(ValueError):
            self.make_motor(step_interval=0.00001, pulse_width=0.001)
        self.request_lines.assert_not_called()


if __name__ == "__main__":
    unittest.main()
