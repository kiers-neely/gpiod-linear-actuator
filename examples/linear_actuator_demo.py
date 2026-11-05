import asyncio
from unittest.mock import MagicMock, patch

from gpiod.line import Value

from gpiod_linear_actuator import load_gpio_config
from gpiod_linear_actuator.protocol import InputLabel
from gpiod_linear_actuator.linearactuator import LinearActuatorControl, LinearActuatorState


async def main():
    with patch("gpiod_linear_actuator.pwm.PWM"):  # no real PWM hardware needed
        config = load_gpio_config("examples/gpio_config.yaml")

        fake_gpio = MagicMock()
        fake_gpio.is_active.return_value = False
        fake_gpio.get.return_value = Value.ACTIVE  # ENA readback for enable_drive()

        actuator = LinearActuatorControl(config, gpio=fake_gpio)
        await actuator.connect()  # claims lines/PWM, wires up ALARM + limit handling

        # normally the limit switch trips on its own via the edge reader;
        # here we fire the same callback directly to simulate it
        async def simulate_limit_trip():
            await asyncio.sleep(0.05)
            actuator._on_limit(InputLabel.MAX_LIMIT)()

        asyncio.create_task(simulate_limit_trip())
        await actuator.start_move(LinearActuatorState.END, max_duration_s=5.0)
        print(f"Reached END, alarm_active={actuator.alarm_active}")

        # an ALARM trip cancels an in-flight move immediately, the same way
        async def simulate_alarm_trip():
            await asyncio.sleep(0.05)
            actuator._on_alarm()

        asyncio.create_task(simulate_alarm_trip())
        await actuator.start_move(LinearActuatorState.HOME, max_duration_s=5.0)
        print(f"Move interrupted, alarm_active={actuator.alarm_active}")

        try:
            await actuator.start_move(LinearActuatorState.HOME)
        except RuntimeError as e:
            print(f"Refused: {e}")  # alarm must be cleared first

        fake_gpio.is_active.return_value = False  # physical ALARM line now clear
        print(f"clear_alarm() -> {actuator.clear_alarm()}")

        await actuator.teardown()


asyncio.run(main())
