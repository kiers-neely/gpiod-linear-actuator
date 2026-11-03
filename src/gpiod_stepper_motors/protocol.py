
import asyncio
from typing import Callable, Iterable, Protocol, Optional, runtime_checkable

import gpiod
from pydantic import PositiveInt


@runtime_checkable
class GPIOBackend(Protocol):
    """The minimum protocol a device needs from whatever is driving its GPIO lines.

    A device takes this so a test can inject an in-memory stand-in (WIP)
    """

    def open(self) -> None:
        """Open the chip and claim every configured line."""

    def close(self) -> None:
        """Release the lines and close the chip. Safe to call twice."""

    def is_active(self, label: str) -> bool:
        """Whether the line is currently asserted."""
        ...

    def set(self, label: str, on: bool) -> None:
        """Assert or release one line."""

    def set_many(self, labels: Iterable[str], on: bool) -> None:
        """Assert or release several lines in one write."""

    def on_assert(self, label: str, callback: Callable[[], None]) -> None:
        """Call `callback` each time the line asserts."""
