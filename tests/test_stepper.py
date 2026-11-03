import unittest
from unittest.mock import MagicMock, patch

import gpiod
from gpiod.line import Direction as LineDirection
from gpiod.line import Value

from gpiod_stepper_motors.pwm import (
    HardwarePWM,
    PWMConfig,
    PWMRampConfig,
    VariableSpeedPWM,
)


class StepperMotorTests(unittest.TestCase):
    """Mocks a GPIO chip/channel at the gpiod layer for future GPIOStepper tests.

    No stepper motor class exists yet (StepperMotor was removed, GPIOStepper is
    still a stub), so these only exercise the request_lines double itself.
    """

    STEP_PIN = 17
    DIR_PIN = 27

    def setUp(self):
        self.request_patcher = patch("gpiod.request_lines")
        self.request_lines = self.request_patcher.start()
        self.addCleanup(self.request_patcher.stop)
        self.request = self.request_lines.return_value

    def make_request(self, chip="/dev/gpiochip0", consumer="gpiod-stepper-motors"):
        config = {
            self.STEP_PIN: gpiod.LineSettings(
                direction=LineDirection.OUTPUT,
                output_value=Value.INACTIVE,
            ),
            self.DIR_PIN: gpiod.LineSettings(
                direction=LineDirection.OUTPUT,
                output_value=Value.INACTIVE,
            ),
        }
        return gpiod.request_lines(chip, consumer=consumer, config=config)

    def test_requests_step_and_direction_outputs_in_inactive_state(self):
        self.make_request()

        self.request_lines.assert_called_once()
        config = self.request_lines.call_args.kwargs["config"]
        self.assertEqual(config[self.STEP_PIN].output_value, Value.INACTIVE)
        self.assertEqual(config[self.DIR_PIN].output_value, Value.INACTIVE)

    def test_mock_request_records_set_value_calls(self):
        request = self.make_request()

        request.set_value(self.DIR_PIN, Value.ACTIVE)
        request.set_value(self.STEP_PIN, Value.ACTIVE)
        request.set_value(self.STEP_PIN, Value.INACTIVE)

        self.assertEqual(
            self.request.set_value.call_args_list,
            [
                unittest.mock.call(self.DIR_PIN, Value.ACTIVE),
                unittest.mock.call(self.STEP_PIN, Value.ACTIVE),
                unittest.mock.call(self.STEP_PIN, Value.INACTIVE),
            ],
        )

    def test_mock_request_release_is_recorded(self):
        request = self.make_request()

        request.release()

        self.request.release.assert_called_once()


class HardwarePWMTests(unittest.TestCase):
    def setUp(self):
        self.pwm_patcher = patch("gpiod_stepper_motors.pwm.PWM")
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


class VariableSpeedPWMTests(unittest.TestCase):
    def setUp(self):
        self.pwm_patcher = patch("gpiod_stepper_motors.pwm.PWM")
        self.pwm_cls = self.pwm_patcher.start()
        self.addCleanup(self.pwm_patcher.stop)
        self.sleep_patcher = patch("gpiod_stepper_motors.pwm.time.sleep")
        self.sleep = self.sleep_patcher.start()
        self.addCleanup(self.sleep_patcher.stop)

    def make_variable_speed_pwm(self, frequency_hz=100.0, target_freq_hz=500.0, ramp_duration_s=1.0):
        config = PWMConfig(label="test", offset=12, frequency_hz=frequency_hz)
        ramp_config = PWMRampConfig(target_freq_hz=target_freq_hz, ramp_duration_s=ramp_duration_s)
        return VariableSpeedPWM(HardwarePWM(config), ramp_config=ramp_config)

    def test_ramp_to_rejects_non_positive_target_frequency(self):
        vsp = self.make_variable_speed_pwm()

        vsp.target_freq = 0
        with self.assertRaises(ValueError):
            vsp.ramp_to()

        vsp.target_freq = -10
        with self.assertRaises(ValueError):
            vsp.ramp_to()

        self.assertIsNone(vsp._sweep_thread)

    def test_ramp_to_rejects_negative_duration(self):
        vsp = self.make_variable_speed_pwm()
        vsp.ramp_duration = -1.0

        with self.assertRaises(ValueError):
            vsp.ramp_to()
        self.assertIsNone(vsp._sweep_thread)

    def test_ramp_to_raises_if_previous_ramp_still_running(self):
        vsp = self.make_variable_speed_pwm()
        stuck_thread = MagicMock()
        stuck_thread.is_alive.return_value = True
        vsp._sweep_thread = stuck_thread

        with self.assertRaises(RuntimeError):
            vsp.ramp_to()

    def test_ramp_to_runs_in_background_thread_and_reaches_target(self):
        vsp = self.make_variable_speed_pwm(frequency_hz=100.0, target_freq_hz=300.0, ramp_duration_s=0.05)

        vsp.ramp_to()
        vsp._sweep_thread.join(timeout=1)

        self.assertFalse(vsp._sweep_thread.is_alive())
        self.assertEqual(vsp.current_freq, 300.0)
        self.assertEqual(self.pwm_cls.return_value.frequency, 300.0)

    def test_run_ramp_interpolates_frequency_linearly(self):
        steps = 3
        vsp = self.make_variable_speed_pwm(
            frequency_hz=100.0, target_freq_hz=400.0, ramp_duration_s=steps / 100
        )

        with patch.object(
            vsp.pwm, "set_frequency", wraps=vsp.pwm.set_frequency
        ) as set_freq:
            vsp._run_ramp()

        expected = [
            unittest.mock.call(100.0 + 300.0 * (i / steps)) for i in range(1, steps + 1)
        ]
        self.assertEqual(set_freq.call_args_list, expected)
        self.assertEqual(vsp.current_freq, 400.0)

    def test_run_ramp_sets_frequency_directly_when_duration_rounds_to_zero_steps(self):
        vsp = self.make_variable_speed_pwm(
            frequency_hz=100.0, target_freq_hz=500.0, ramp_duration_s=0.001
        )

        vsp._run_ramp()

        self.assertEqual(self.pwm_cls.return_value.frequency, 500.0)
        self.assertEqual(vsp.current_freq, 500.0)

    def test_run_ramp_stops_immediately_when_cancelled(self):
        vsp = self.make_variable_speed_pwm(frequency_hz=100.0, target_freq_hz=500.0, ramp_duration_s=1.0)
        vsp._cancel_event.set()

        with patch.object(vsp.pwm, "set_frequency") as set_freq:
            vsp._run_ramp()

        set_freq.assert_not_called()

    def test_run_ramp_stops_when_pwm_reports_fault(self):
        vsp = self.make_variable_speed_pwm(frequency_hz=100.0, target_freq_hz=500.0, ramp_duration_s=1.0)
        vsp.pwm._fault.set()

        with patch.object(vsp.pwm, "set_frequency") as set_freq:
            vsp._run_ramp()

        set_freq.assert_not_called()
        self.assertTrue(vsp._cancel_event.is_set())

    def test_stop_sweep_returns_true_when_no_thread_running(self):
        vsp = self.make_variable_speed_pwm()

        self.assertTrue(vsp.stop_sweep())
        self.assertTrue(vsp._cancel_event.is_set())

    def test_stop_sweep_returns_false_when_join_raises(self):
        vsp = self.make_variable_speed_pwm()
        broken_thread = MagicMock()
        broken_thread.is_alive.return_value = True
        broken_thread.join.side_effect = RuntimeError("boom")
        vsp._sweep_thread = broken_thread

        self.assertFalse(vsp.stop_sweep())


if __name__ == "__main__":
    unittest.main()
