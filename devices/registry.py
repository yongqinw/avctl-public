"""The registry: config names a class, this finds and validates it.

A device entry in config.yaml says WHICH driver runs it by bare class
name -- `driver: ToppingD900` -- and nothing else. Which file the class
lives in is an implementation detail the config never mentions: the
registry imports every module in the category's package and looks for a
concrete subclass of the category's interface with that name. Rename a
driver's file tomorrow and no config changes.

Everything is validated at BOOT (validate() runs from the app's
lifespan), so a typo produces a readable error naming the category, the
bad string and what actually exists -- not an AttributeError mid-scene
at 2am. A category with no `driver:` key keeps its default, so today's
config keeps working unchanged.

Instances are cached: every part of the app that asks for a category's
device gets THE one, sharing its locks -- never a second copy of a
serial port or a state file.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
import threading
from typing import Any

from devices import config as device_config
from devices.amp import Amp
from devices.dac import Dac
from devices.music import MusicSource
from devices.tv import Tv


class RegistryError(RuntimeError):
    """Config names a driver this rack does not have. Raised at boot."""


# category -> (package name, interface, default driver class name). The
# config block is named after the CATEGORY -- music:, not applemusic: --
# because the block outlives any one driver; which driver is its business
# is exactly one line inside it.
CATEGORIES: dict[str, tuple[str, type, str]] = {
    "dac": ("devices.dac", Dac, "ToppingD900"),
    "amp": ("devices.amp", Amp, "McIntoshMAC7200"),
    "tv": ("devices.tv", Tv, "LGTelevision"),
    "music": ("devices.music", MusicSource, "AppleMusic"),
}

_LOCK = threading.Lock()
_classes: dict[str, type] = {}
_instances: dict[str, Any] = {}


def _drivers_in(package_name: str, interface: type) -> dict[str, type]:
    """Every concrete implementation the category's folder offers."""
    package = importlib.import_module(package_name)
    found: dict[str, type] = {}
    for module_info in pkgutil.iter_modules(package.__path__):
        module = importlib.import_module(f"{package_name}.{module_info.name}")
        for name, cls in vars(module).items():
            if (inspect.isclass(cls) and issubclass(cls, interface)
                    and not inspect.isabstract(cls)
                    and cls.__module__ == module.__name__):
                found[name] = cls
    return found


def driver_class(category: str) -> type:
    """The class config names for this category, resolved and checked."""
    with _LOCK:
        if category in _classes:
            return _classes[category]
    package_name, interface, default = CATEGORIES[category]
    config = device_config.load_config()
    try:
        name = device_config.configured_driver(
            config, category, default)
    except ValueError as exc:
        raise RegistryError(str(exc)) from None
    cls = named_driver_class(category, name)
    with _LOCK:
        _classes[category] = cls
    return cls


def named_driver_class(category: str, name: str) -> type:
    """Resolve one available driver by name without changing the cache."""
    package_name, interface, _default = CATEGORIES[category]
    available = _drivers_in(package_name, interface)
    cls = available.get(name)
    if cls is None:
        options = ", ".join(sorted(available)) or "none at all"
        raise RegistryError(
            f"{category}.driver names {name!r}, but {package_name}/ has "
            f"no such {interface.__name__} -- found: {options}"
        )
    return cls


def install(category: str, cls: type, instance: Any) -> None:
    """Atomically replace one category after an explicit live switch."""
    package_name, interface, _default = CATEGORIES[category]
    if not isinstance(instance, interface) or not issubclass(cls, interface):
        raise RegistryError(
            f"{cls.__name__} is not a {interface.__name__} from {package_name}")
    with _LOCK:
        _classes[category] = cls
        _instances[category] = instance


def device(category: str, **kwargs: Any) -> Any:
    """THE configured instance for a category, built once via the driver's
    from_config and shared by everyone who asks."""
    with _LOCK:
        if category in _instances:
            return _instances[category]
    # Resolved OUTSIDE the lock -- driver_class takes _LOCK itself.
    cls = driver_class(category)
    with _LOCK:
        # Checked again: another thread may have built it while the class
        # resolved. Construction happens UNDER the lock (#95): today's
        # drivers are side-effect-free in from_config, but the promise is
        # "never a second copy of a serial port or a state file", and a
        # loser built outside the lock was discarded with no teardown.
        if category not in _instances:
            _instances[category] = cls.from_config(
                device_config.load_config(), **kwargs)
        return _instances[category]


def validate() -> list[str]:
    """Resolve every category; returns one human line each. Called from
    the lifespan so a bad driver name stops the boot with its reason,
    instead of surfacing as a 502 the first time a button needs it."""
    lines = []
    for category in CATEGORIES:
        cls = driver_class(category)
        lines.append(f"{category:<6}{cls.__name__}")
    return lines


def reset() -> None:
    """Forget resolutions and instances -- for tests that swap configs."""
    with _LOCK:
        _classes.clear()
        _instances.clear()
    # Roon projects one shared session into several legacy categories. A
    # registry reset must forget it too or a config/test switch retains the
    # previous Core behind freshly constructed music/DAC/amp drivers.
    from devices.roon.factory import reset as reset_roon
    reset_roon()
