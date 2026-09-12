"""Share one Roon session across media, DAC and amplifier projections."""

from __future__ import annotations

import threading
from typing import Any

from devices.roon.contracts import RoonController, RoonError
from devices.roon.mock import MockRoonController

_LOCK = threading.Lock()
_CONTROLLER: RoonController | None = None


def controller_from_config(config: dict[str, Any]) -> RoonController:
    global _CONTROLLER
    with _LOCK:
        if _CONTROLLER is not None:
            return _CONTROLLER
        block = config.get("roon") or {}
        mode = str(block.get("mode") or "live")
        if mode == "mock":
            _CONTROLLER = MockRoonController()
            return _CONTROLLER
        if mode == "live":
            from devices.roon.live import PyRoonController
            _CONTROLLER = PyRoonController.from_config(config)
            return _CONTROLLER
        raise RoonError(f"unknown roon.mode: {mode!r}")


def reset() -> None:
    global _CONTROLLER
    with _LOCK:
        previous = _CONTROLLER
        _CONTROLLER = None
    close = getattr(previous, "close", None)
    if callable(close):
        close()
