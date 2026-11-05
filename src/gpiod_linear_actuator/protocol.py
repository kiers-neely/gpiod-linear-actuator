from collections.abc import Callable
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class InputLabel(StrEnum):
    ALARM = "ALARM"
    MAX_LIMIT = "MAX_LIMIT"
    MIN_LIMIT = "MIN_LIMIT"


class OutputLabel(StrEnum):
    PUL = "PUL"
    DIR = "DIR"
    ENA = "ENA"


class GPIOValue(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


@runtime_checkable
class GPIOBackend(Protocol):
    """The minimum protocol a device needs from whatever is driving its GPIO lines.

    A device takes this so a test can inject an in-memory stand-in (WIP)
    """

    def open(self) -> None:
        """Open all the necessary resources for this device."""

    def close(self) -> None:
        """Release/close all resources. Safe to call twice."""

    def is_active(self, line: InputLabel | OutputLabel | int) -> bool:
        """Whether the line is currently asserted."""
        ...

    def set(self, line: InputLabel | OutputLabel | int, value: GPIOValue) -> None:
        """Assert or release one or more GPIO lines."""
        ...

    def get(self, line: InputLabel | OutputLabel | int) -> Any:
        """Poll the current logical state of one or more GPIO lines."""

    def on_assert(self, line: InputLabel | int, callback: Callable[[], None]) -> None:
        """Define a `callback` to be called each time an edge event is read."""
