import asyncio
import unittest
from collections.abc import Iterable
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import gpiod
from gpiod.line import Direction as LineDirection
from gpiod.line import Value
from pydantic import ValidationError

from gpiod_linear_actuator.protocol import GPIOValue, InputLabel, OutputLabel
from gpiod_linear_actuator.pwm import HardwarePWM, PWMConfig, PWMRampConfig
from gpiod_linear_actuator.stepper import (
    GPIOConfig,
    GPIOStepperDriver,
    InputLine,
    LinearActuatorControl,
    LinearActuatorState,
    OutputLine,
    Pull,
)


class StepperMotorTests(unittest.TestCase):
    """Exercises a mock GPIO chip/channel at the raw gpiod layer.

    Predates `GPIODevice` (see `GPIODeviceTests` below for that); kept as a
    minimal sanity check of the `gpiod.request_lines` mocking pattern used
    throughout this file.
    """

    STEP_PIN = 17
    DIR_PIN = 27

    def setUp(self):
        self.request_patcher = patch("gpiod.request_lines")
        self.request_lines = self.request_patcher.start()
        self.addCleanup(self.request_patcher.stop)
        self.request = self.request_lines.return_value

    def make_request(self, chip="/dev/gpiochip0", consumer="gpiod-linear-actuator"):
        config: dict[Iterable[int | str] | int | str, gpiod.LineSettings | None] = {
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
                call(self.DIR_PIN, Value.ACTIVE),
                call(self.STEP_PIN, Value.ACTIVE),
                call(self.STEP_PIN, Value.INACTIVE),
            ],
        )

    def test_mock_request_release_is_recorded(self):
        request = self.make_request()

        request.release()

        self.request.release.assert_called_once()


class PWMRampConfigTests(unittest.TestCase):
    def test_rejects_non_positive_target_frequency(self):
        with self.assertRaises(ValidationError):
            PWMRampConfig(target_freq_hz=0, ramp_duration_s=1.0)
        with self.assertRaises(ValidationError):
            PWMRampConfig(target_freq_hz=-10, ramp_duration_s=1.0)

    def test_rejects_negative_duration(self):
        with self.assertRaises(ValidationError):
            PWMRampConfig(target_freq_hz=500.0, ramp_duration_s=-1.0)


class HardwarePWMTests(unittest.TestCase):
    def setUp(self):
        self.pwm_patcher = patch("gpiod_linear_actuator.pwm.PWM")
        self.pwm_cls = self.pwm_patcher.start()
        self.addCleanup(self.pwm_patcher.stop)
        self.pwm = self.pwm_cls.return_value
        self.sleep_patcher = patch("gpiod_linear_actuator.pwm.time.sleep")
        self.sleep = self.sleep_patcher.start()
        self.addCleanup(self.sleep_patcher.stop)

    def make_pwm(self, **kwargs):
        config = PWMConfig(label="test", offset=12, **kwargs)
        return HardwarePWM(config)

    def make_ramping_pwm(
        self, frequency_hz=100.0, target_freq_hz=500.0, ramp_duration_s=1.0
    ):
        return self.make_pwm(
            frequency_hz=frequency_hz,
            ramp_config=PWMRampConfig(
                target_freq_hz=target_freq_hz, ramp_duration_s=ramp_duration_s
            ),
        )

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

    def test_set_frequency_updates_current_freq(self):
        pwm = self.make_pwm(frequency_hz=100.0)

        pwm.set_frequency(250.0)

        self.assertEqual(pwm.current_freq, 250.0)

    def test_set_duty_cycle_rejects_out_of_range_values(self):
        pwm = self.make_pwm()

        with self.assertRaises(ValueError):
            pwm.set_duty_cycle(-0.1)
        with self.assertRaises(ValueError):
            pwm.set_duty_cycle(1.1)

    def test_start_pwm_without_ramp_config_just_enables(self):
        pwm = self.make_pwm(frequency_hz=500.0)

        pwm.start_pwm()

        self.pwm.enable.assert_called_once()
        self.assertEqual(self.pwm.frequency, 500.0)
        self.assertIsNone(pwm._sweep_thread)

    def test_start_pwm_with_ramp_config_enables_and_ramps_to_target(self):
        pwm = self.make_ramping_pwm(
            frequency_hz=100.0, target_freq_hz=300.0, ramp_duration_s=0.05
        )

        pwm.start_pwm()

        self.pwm.enable.assert_called_once()
        assert pwm._sweep_thread is not None
        pwm._sweep_thread.join(timeout=1)
        self.assertFalse(pwm._sweep_thread.is_alive())
        self.assertEqual(pwm.current_freq, 300.0)
        self.assertEqual(self.pwm.frequency, 300.0)

    def test_start_pwm_raises_if_previous_ramp_still_running(self):
        pwm = self.make_ramping_pwm()
        stuck_thread = MagicMock()
        stuck_thread.is_alive.return_value = True
        pwm._sweep_thread = stuck_thread

        with self.assertRaises(RuntimeError):
            pwm.start_pwm()

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

    def test_stop_pwm_stops_sweep_before_disabling_hardware(self):
        pwm = self.make_pwm()
        manager = MagicMock()
        manager.attach_mock(patch.object(pwm, "_stop_sweep").start(), "stop_sweep")
        manager.attach_mock(patch.object(self.pwm, "disable").start(), "disable")
        self.addCleanup(patch.stopall)

        pwm.stop_pwm()

        manager.stop_sweep.assert_called_once()
        manager.disable.assert_called_once()
        self.assertEqual([c[0] for c in manager.mock_calls], ["stop_sweep", "disable"])

    def test_stop_pwm_stops_an_in_progress_ramp_thread(self):
        pwm = self.make_ramping_pwm(
            frequency_hz=100.0, target_freq_hz=2000.0, ramp_duration_s=1.0
        )
        pwm.start_pwm()
        assert pwm._sweep_thread is not None

        pwm.stop_pwm()

        self.assertFalse(pwm._sweep_thread.is_alive())
        self.pwm.disable.assert_called_once()

    def test_run_ramp_interpolates_frequency_linearly(self):
        steps = 3
        pwm = self.make_pwm(frequency_hz=100.0)
        ramp_config = PWMRampConfig(target_freq_hz=400.0, ramp_duration_s=steps / 100)

        with patch.object(pwm, "set_frequency", wraps=pwm.set_frequency) as set_freq:
            pwm._run_ramp(ramp_config)

        expected = [call(100.0 + 300.0 * (i / steps)) for i in range(1, steps + 1)]
        self.assertEqual(set_freq.call_args_list, expected)
        self.assertEqual(pwm.current_freq, 400.0)

    def test_run_ramp_sets_frequency_directly_when_duration_rounds_to_zero_steps(self):
        pwm = self.make_pwm(frequency_hz=100.0)
        ramp_config = PWMRampConfig(target_freq_hz=500.0, ramp_duration_s=0.001)

        pwm._run_ramp(ramp_config)

        self.assertEqual(self.pwm.frequency, 500.0)
        self.assertEqual(pwm.current_freq, 500.0)

    def test_run_ramp_stops_immediately_when_cancelled(self):
        pwm = self.make_pwm(frequency_hz=100.0)
        ramp_config = PWMRampConfig(target_freq_hz=500.0, ramp_duration_s=1.0)
        pwm._cancel_event.set()

        with patch.object(pwm, "set_frequency") as set_freq:
            pwm._run_ramp(ramp_config)

        set_freq.assert_not_called()

    def test_run_ramp_stops_when_pwm_reports_fault(self):
        pwm = self.make_pwm(frequency_hz=100.0)
        ramp_config = PWMRampConfig(target_freq_hz=500.0, ramp_duration_s=1.0)
        pwm._fault.set()

        with patch.object(pwm, "set_frequency") as set_freq:
            pwm._run_ramp(ramp_config)

        set_freq.assert_not_called()
        self.assertTrue(pwm._cancel_event.is_set())

    def test_stop_sweep_returns_true_when_no_thread_running(self):
        pwm = self.make_pwm()

        self.assertTrue(pwm._stop_sweep())
        self.assertTrue(pwm._cancel_event.is_set())

    def test_stop_sweep_returns_false_when_join_raises(self):
        pwm = self.make_pwm()
        broken_thread = MagicMock()
        broken_thread.is_alive.return_value = True
        broken_thread.join.side_effect = RuntimeError("boom")
        pwm._sweep_thread = broken_thread

        self.assertFalse(pwm._stop_sweep())

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

    def test_close_hardware_stops_sweep_and_closes_underlying_pwm(self):
        pwm = self.make_ramping_pwm(
            frequency_hz=100.0, target_freq_hz=2000.0, ramp_duration_s=1.0
        )
        pwm.start_pwm()
        assert pwm._sweep_thread is not None

        pwm.close_hardware()

        self.assertFalse(pwm._sweep_thread.is_alive())
        self.pwm.close.assert_called_once()


class GPIODeviceTests(unittest.TestCase):
    def setUp(self):
        self.chip_patcher = patch("gpiod_linear_actuator.stepper.Chip")
        self.chip_cls = self.chip_patcher.start()
        self.addCleanup(self.chip_patcher.stop)
        self.chip = self.chip_cls.return_value.__enter__.return_value
        self.request = self.chip.request_lines.return_value
        self.request.chip_name = "gpiochip0"

    def default_pin_mapping(self):
        return [
            OutputLine(label=OutputLabel.PUL, offset=17),
            InputLine(label=InputLabel.ALARM, offset=23, pull=Pull.UP),
        ]

    def make_device(self, pin_mapping=None):
        return GPIOStepperDriver(
            chip_path="/dev/gpiochip0",
            pin_mapping=(
                pin_mapping if pin_mapping is not None else self.default_pin_mapping()
            ),
        )

    def test_claim_lines_requests_combined_input_and_output_config(self):
        device = self.make_device()

        device.claim_lines()

        config = self.chip.request_lines.call_args.kwargs["config"]
        self.assertEqual(config[17].direction, LineDirection.OUTPUT)
        self.assertEqual(config[23].direction, LineDirection.INPUT)

    def test_open_stores_claimed_line_request(self):
        device = self.make_device()

        device.open()

        self.assertIs(device.line_request, self.request)

    def test_open_wraps_claim_failure_and_cleans_up(self):
        self.chip.request_lines.side_effect = RuntimeError("boom")
        device = self.make_device()

        with self.assertRaises(RuntimeError):
            device.open()

        self.assertIsNone(device.line_request)

    def test_set_by_label_resolves_offset(self):
        device = self.make_device()
        device.open()

        device.set(OutputLabel.PUL, GPIOValue.ACTIVE)

        self.request.set_value.assert_called_once_with(17, Value.ACTIVE)

    def test_set_by_offset_passes_through(self):
        device = self.make_device()
        device.open()

        device.set(17, GPIOValue.INACTIVE)

        self.request.set_value.assert_called_once_with(17, Value.INACTIVE)

    def test_set_unknown_label_raises_key_error(self):
        device = self.make_device()
        device.open()

        with self.assertRaises(KeyError):
            device.set(OutputLabel.DIR, GPIOValue.ACTIVE)

    def test_get_by_label_returns_value(self):
        device = self.make_device()
        device.open()
        self.request.get_value.return_value = Value.ACTIVE

        self.assertEqual(device.get(InputLabel.ALARM), Value.ACTIVE)
        self.request.get_value.assert_called_once_with(23)

    def test_get_unknown_label_raises_key_error(self):
        device = self.make_device()
        device.open()

        with self.assertRaises(KeyError):
            device.get(OutputLabel.DIR)

    def test_is_active_reflects_line_value(self):
        device = self.make_device()
        device.open()

        self.request.get_value.return_value = Value.ACTIVE
        self.assertTrue(device.is_active(InputLabel.ALARM))

        self.request.get_value.return_value = Value.INACTIVE
        self.assertFalse(device.is_active(InputLabel.ALARM))

    def test_close_releases_and_clears_request(self):
        device = self.make_device()
        device.open()

        device.close()

        self.request.release.assert_called_once()
        self.assertIsNone(device.line_request)

    def test_close_is_safe_before_open(self):
        device = self.make_device()

        device.close()  # must not raise

        self.assertIsNone(device.line_request)

    def test_close_is_safe_when_called_twice(self):
        device = self.make_device()
        device.open()

        device.close()
        device.close()  # must not raise

        self.request.release.assert_called_once()


class GPIODeviceEdgeReaderTests(unittest.TestCase):
    def setUp(self):
        self.chip_patcher = patch("gpiod_linear_actuator.stepper.Chip")
        self.chip_cls = self.chip_patcher.start()
        self.addCleanup(self.chip_patcher.stop)
        self.chip = self.chip_cls.return_value.__enter__.return_value
        self.request = self.chip.request_lines.return_value
        self.request.chip_name = "gpiochip0"
        self.request.fd = 7

    def make_device(self):
        device = GPIOStepperDriver(
            chip_path="/dev/gpiochip0",
            pin_mapping=[InputLine(label=InputLabel.ALARM, offset=23, pull=Pull.UP)],
        )
        device.open()
        return device

    def make_event(self, offset):
        return SimpleNamespace(line_offset=offset)

    def test_on_assert_registers_callback_by_label(self):
        device = self.make_device()
        callback = MagicMock()

        device.on_assert(InputLabel.ALARM, callback)

        self.assertIs(device._callbacks_by_offset[23], callback)

    def test_on_assert_registers_callback_by_offset(self):
        device = self.make_device()
        callback = MagicMock()

        device.on_assert(23, callback)

        self.assertIs(device._callbacks_by_offset[23], callback)

    def test_on_assert_unknown_label_raises_key_error(self):
        device = self.make_device()

        with self.assertRaises(KeyError):
            device.on_assert(InputLabel.MAX_LIMIT, MagicMock())

    def test_handle_edge_event_invokes_matching_callback(self):
        device = self.make_device()
        callback = MagicMock()
        device.on_assert(InputLabel.ALARM, callback)

        device._handle_edge_event(self.make_event(23))

        callback.assert_called_once()

    def test_handle_edge_event_noop_when_no_callback_registered(self):
        device = self.make_device()

        device._handle_edge_event(self.make_event(23))  # must not raise

    def test_handle_edge_event_logs_and_continues_when_callback_raises(self):
        device = self.make_device()
        device.on_assert(InputLabel.ALARM, MagicMock(side_effect=RuntimeError("boom")))

        device._handle_edge_event(self.make_event(23))  # must not raise

    def test_monitor_fd_dispatches_all_pending_events(self):
        device = self.make_device()
        callback = MagicMock()
        device.on_assert(InputLabel.ALARM, callback)
        self.request.read_edge_events.return_value = [
            self.make_event(23),
            self.make_event(23),
        ]

        device._monitor_fd()

        self.assertEqual(callback.call_count, 2)

    def test_start_reader_registers_monitor_fd_with_running_loop(self):
        device = self.make_device()
        fake_loop = MagicMock()

        with patch("asyncio.get_running_loop", return_value=fake_loop):
            device._start_reader()

        fake_loop.add_reader.assert_called_once_with(
            self.request.fd, device._monitor_fd
        )
        self.assertIs(device._loop, fake_loop)

    def test_start_reader_raises_if_lines_not_claimed(self):
        device = GPIOStepperDriver(
            chip_path="/dev/gpiochip0",
            pin_mapping=[InputLine(label=InputLabel.ALARM, offset=23, pull=Pull.UP)],
        )

        with self.assertRaises(RuntimeError):
            device._start_reader()

    def test_stop_reader_removes_registered_fd(self):
        device = self.make_device()
        fake_loop = MagicMock()
        with patch("asyncio.get_running_loop", return_value=fake_loop):
            device._start_reader()

        device._stop_reader()

        fake_loop.remove_reader.assert_called_once_with(self.request.fd)
        self.assertIsNone(device._loop)

    def test_stop_reader_is_safe_without_starting(self):
        device = self.make_device()

        device._stop_reader()  # must not raise

    def test_close_releases_lines_even_if_stop_reader_fails(self):
        device = self.make_device()
        fake_loop = MagicMock()
        fake_loop.remove_reader.side_effect = RuntimeError("boom")
        with patch("asyncio.get_running_loop", return_value=fake_loop):
            device._start_reader()

        device.close()

        self.request.release.assert_called_once()
        self.assertIsNone(device.line_request)


class GPIOStepperTests(unittest.TestCase):
    def setUp(self):
        self.pwm_patcher = patch("gpiod_linear_actuator.pwm.PWM")
        self.pwm_patcher.start()
        self.addCleanup(self.pwm_patcher.stop)

    def make_config(self):
        return GPIOConfig(
            name="test",
            pin_mapping=[OutputLine(label=OutputLabel.PUL, offset=17)],
            pwm=PWMConfig(label="step", offset=17),
        )

    def test_uses_provided_gpio_backend_when_given(self):
        fake_backend = MagicMock()

        stepper = LinearActuatorControl(self.make_config(), gpio=fake_backend)

        self.assertIs(stepper._gpio, fake_backend)

    def test_constructs_gpio_device_when_backend_omitted(self):
        stepper = LinearActuatorControl(self.make_config())

        self.assertIsInstance(stepper._gpio, GPIOStepperDriver)


class LinearActuatorControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pwm_patcher = patch("gpiod_linear_actuator.pwm.PWM")
        self.pwm_cls = self.pwm_patcher.start()
        self.addCleanup(self.pwm_patcher.stop)
        self.sleep_patcher = patch("gpiod_linear_actuator.pwm.time.sleep")
        self.sleep_patcher.start()
        self.addCleanup(self.sleep_patcher.stop)

        self.fake_gpio = MagicMock()
        self.fake_gpio.is_active.return_value = False

    def make_config(self, **pwm_kwargs):
        return GPIOConfig(
            name="x-axis",
            pin_mapping=[
                OutputLine(label=OutputLabel.PUL, offset=17),
                OutputLine(label=OutputLabel.DIR, offset=27),
                OutputLine(label=OutputLabel.ENA, offset=22),
                InputLine(label=InputLabel.MAX_LIMIT, offset=23, pull=Pull.UP),
                InputLine(label=InputLabel.MIN_LIMIT, offset=24, pull=Pull.UP),
                InputLine(label=InputLabel.ALARM, offset=25, pull=Pull.UP),
            ],
            pwm=PWMConfig(label="step", offset=17, **pwm_kwargs),
        )

    async def make_connected_driver(self, **pwm_kwargs):
        driver = LinearActuatorControl(self.make_config(**pwm_kwargs), gpio=self.fake_gpio)
        await driver.connect()
        return driver

    # -- connect() --

    async def test_connect_registers_callbacks_and_seeds_limits(self):
        self.fake_gpio.is_active.side_effect = lambda label: label == InputLabel.MIN_LIMIT
        driver = LinearActuatorControl(self.make_config(), gpio=self.fake_gpio)

        await driver.connect()

        self.assertTrue(driver.connected)
        registered_labels = {c.args[0] for c in self.fake_gpio.on_assert.call_args_list}
        self.assertEqual(
            registered_labels, {InputLabel.MAX_LIMIT, InputLabel.MIN_LIMIT, InputLabel.ALARM}
        )
        self.assertEqual(
            driver._limit_reached,
            {InputLabel.MIN_LIMIT: True, InputLabel.MAX_LIMIT: False},
        )
        self.assertIsInstance(driver._pwm, HardwarePWM)

    async def test_connect_does_not_start_reader_for_injected_backend(self):
        driver = LinearActuatorControl(self.make_config(), gpio=self.fake_gpio)

        await driver.connect()

        self.fake_gpio._start_reader.assert_not_called()

    # -- _on_alarm / _on_limit --

    async def test_on_alarm_sets_flag_and_cancels_current_move(self):
        driver = LinearActuatorControl(self.make_config(), gpio=self.fake_gpio)
        mock_task = MagicMock()
        driver._current_move = mock_task

        driver._on_alarm()

        self.assertTrue(driver.alarm_active)
        mock_task.cancel.assert_called_once()

    async def test_on_limit_sets_flag_and_cancels_current_move(self):
        driver = LinearActuatorControl(self.make_config(), gpio=self.fake_gpio)
        mock_task = MagicMock()
        driver._current_move = mock_task

        driver._on_limit(InputLabel.MAX_LIMIT)()

        self.assertTrue(driver._limit_reached[InputLabel.MAX_LIMIT])
        mock_task.cancel.assert_called_once()

    # -- enable_drive / disable_drive --

    async def test_enable_drive_succeeds_when_readback_matches(self):
        driver = await self.make_connected_driver()
        self.fake_gpio.get.return_value = Value.ACTIVE

        result = driver.enable_drive()

        self.assertTrue(result)
        self.assertTrue(driver.drive_enabled)
        self.fake_gpio.set.assert_called_with(OutputLabel.ENA, GPIOValue.ACTIVE)

    async def test_enable_drive_rollback_preserves_prior_state_on_mismatch(self):
        driver = await self.make_connected_driver()
        self.fake_gpio.get.side_effect = [Value.ACTIVE, Value.INACTIVE]

        result = driver.enable_drive()

        self.assertFalse(result)
        self.assertTrue(driver.drive_enabled)  # rolled back to bool(prev) == True

    async def test_disable_drive_succeeds_when_readback_matches(self):
        driver = await self.make_connected_driver()
        driver._drive_enabled = True
        self.fake_gpio.get.return_value = Value.INACTIVE

        result = driver.disable_drive()

        self.assertTrue(result)
        self.assertFalse(driver.drive_enabled)
        self.fake_gpio.set.assert_called_with(OutputLabel.ENA, GPIOValue.INACTIVE)

    async def test_disable_drive_rollback_preserves_prior_state_on_mismatch(self):
        driver = await self.make_connected_driver()
        driver._drive_enabled = True
        self.fake_gpio.get.side_effect = [Value.ACTIVE, Value.ACTIVE]

        result = driver.disable_drive()

        self.assertFalse(result)
        self.assertTrue(driver.drive_enabled)  # rolled back to bool(prev) == True

    # -- start_move() validation --

    async def test_start_move_rejects_invalid_target(self):
        driver = LinearActuatorControl(self.make_config(), gpio=self.fake_gpio)

        with self.assertRaises(ValueError):
            await driver.start_move(LinearActuatorState.MOVING)

    async def test_start_move_requires_connection(self):
        driver = LinearActuatorControl(self.make_config(), gpio=self.fake_gpio)

        with self.assertRaises(RuntimeError):
            await driver.start_move(LinearActuatorState.END)

    async def test_start_move_refuses_while_alarm_active(self):
        driver = await self.make_connected_driver()
        driver._alarm_active = True

        with self.assertRaises(RuntimeError):
            await driver.start_move(LinearActuatorState.END)

    async def test_start_move_refuses_when_already_moving(self):
        driver = await self.make_connected_driver()
        stuck_task = MagicMock()
        stuck_task.done.return_value = False
        driver._current_move = stuck_task

        with self.assertRaises(RuntimeError):
            await driver.start_move(LinearActuatorState.END)

    async def test_start_move_returns_immediately_when_already_at_target(self):
        driver = await self.make_connected_driver()
        self.fake_gpio.is_active.side_effect = lambda label: label == InputLabel.MAX_LIMIT

        await driver.start_move(LinearActuatorState.END)

        self.pwm_cls.return_value.enable.assert_not_called()
        self.assertIsNone(driver._current_move)

    # -- start_move() / _run_move() behavior --

    async def test_start_move_stops_cleanly_on_limit_trip(self):
        driver = await self.make_connected_driver()
        driver._drive_enabled = True

        async def trip_soon():
            await asyncio.sleep(0.01)
            driver._on_limit(InputLabel.MAX_LIMIT)()

        asyncio.create_task(trip_soon())
        await driver.start_move(LinearActuatorState.END, max_duration_s=5.0)

        self.assertIsNone(driver._current_move)
        self.assertTrue(driver._limit_reached[InputLabel.MAX_LIMIT])
        self.fake_gpio.set.assert_any_call(OutputLabel.DIR, GPIOValue.ACTIVE)
        self.pwm_cls.return_value.disable.assert_called_once()

    async def test_start_move_stops_cleanly_on_alarm(self):
        driver = await self.make_connected_driver()
        driver._drive_enabled = True

        async def trip_soon():
            await asyncio.sleep(0.01)
            driver._on_alarm()

        asyncio.create_task(trip_soon())
        await driver.start_move(LinearActuatorState.HOME, max_duration_s=5.0)

        self.assertIsNone(driver._current_move)
        self.assertTrue(driver.alarm_active)
        self.fake_gpio.set.assert_any_call(OutputLabel.DIR, GPIOValue.INACTIVE)
        self.pwm_cls.return_value.disable.assert_called_once()

    async def test_start_move_raises_timeout_error_when_nothing_trips(self):
        driver = await self.make_connected_driver()
        driver._drive_enabled = True

        with self.assertRaises(TimeoutError):
            await driver.start_move(LinearActuatorState.END, max_duration_s=0.02)

        self.assertIsNone(driver._current_move)
        self.pwm_cls.return_value.disable.assert_called_once()

    async def test_start_move_enables_drive_and_settles_when_not_enabled(self):
        driver = await self.make_connected_driver()
        self.fake_gpio.get.return_value = Value.ACTIVE

        async def trip_soon():
            await asyncio.sleep(0.15)
            driver._on_limit(InputLabel.MAX_LIMIT)()

        asyncio.create_task(trip_soon())
        await driver.start_move(LinearActuatorState.END, max_duration_s=5.0)

        self.assertTrue(driver.drive_enabled)
        self.fake_gpio.set.assert_any_call(OutputLabel.ENA, GPIOValue.ACTIVE)

    async def test_start_move_skips_enable_when_drive_already_enabled(self):
        driver = await self.make_connected_driver()
        driver._drive_enabled = True
        self.fake_gpio.reset_mock()

        async def trip_soon():
            await asyncio.sleep(0.01)
            driver._on_limit(InputLabel.MAX_LIMIT)()

        asyncio.create_task(trip_soon())
        await driver.start_move(LinearActuatorState.END, max_duration_s=5.0)

        ena_calls = [
            c for c in self.fake_gpio.set.call_args_list if c.args[0] == OutputLabel.ENA
        ]
        self.assertEqual(ena_calls, [])

    async def test_start_move_can_be_stopped_by_user_via_stop_move(self):
        driver = await self.make_connected_driver()
        driver._drive_enabled = True

        move_task = asyncio.create_task(
            driver.start_move(LinearActuatorState.END, max_duration_s=5.0)
        )
        await asyncio.sleep(0.01)

        stopped = await driver.stop_move()
        await move_task

        self.assertTrue(stopped)
        self.assertIsNone(driver._current_move)
        self.assertFalse(driver._limit_reached.get(InputLabel.MAX_LIMIT, False))

    # -- stop_move() --

    async def test_stop_move_with_no_active_move_still_stops_pwm(self):
        driver = await self.make_connected_driver()

        result = await driver.stop_move()

        self.assertTrue(result)
        self.pwm_cls.return_value.disable.assert_called_once()

    async def test_stop_move_returns_false_when_pwm_raises(self):
        driver = await self.make_connected_driver()
        self.pwm_cls.return_value.disable.side_effect = RuntimeError("boom")

        result = await driver.stop_move()

        self.assertFalse(result)

    # -- refresh_limits() --

    async def test_refresh_limits_updates_both_limits_from_gpio(self):
        driver = LinearActuatorControl(self.make_config(), gpio=self.fake_gpio)
        self.fake_gpio.is_active.side_effect = lambda label: label == InputLabel.MIN_LIMIT

        result = await driver.refresh_limits()

        self.assertEqual(
            result, {InputLabel.MIN_LIMIT: True, InputLabel.MAX_LIMIT: False}
        )

    # -- clear_alarm() --

    async def test_clear_alarm_refuses_while_line_active(self):
        driver = LinearActuatorControl(self.make_config(), gpio=self.fake_gpio)
        driver._alarm_active = True
        self.fake_gpio.is_active.return_value = True

        self.assertFalse(driver.clear_alarm())
        self.assertTrue(driver.alarm_active)

    async def test_clear_alarm_succeeds_once_line_inactive(self):
        driver = LinearActuatorControl(self.make_config(), gpio=self.fake_gpio)
        driver._alarm_active = True
        self.fake_gpio.is_active.return_value = False

        self.assertTrue(driver.clear_alarm())
        self.assertFalse(driver.alarm_active)

    # -- teardown() --

    async def test_teardown_stops_move_and_closes_everything(self):
        driver = await self.make_connected_driver()

        await driver.teardown()

        self.pwm_cls.return_value.disable.assert_called_once()
        self.pwm_cls.return_value.close.assert_called_once()
        self.fake_gpio.close.assert_called_once()

    async def test_teardown_safe_when_never_connected(self):
        driver = LinearActuatorControl(self.make_config(), gpio=self.fake_gpio)

        await driver.teardown()  # must not raise

        self.fake_gpio.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
