"""Device control classes.

Each module wraps one piece of the rack and hides its protocol. Re-exported
here so callers can `from devices import LGTelevision` without knowing which
file it lives in.

The classes take plain values rather than reading configuration files
themselves; loading lives in devices.config, so there is one place that knows
where the files are. LGTelevision.from_config() is the shortcut for the
common case.
"""

from devices.amp import AmpError, McIntoshMAC7200
from devices.blaster import BlasterError, ITachIR
from devices.bluray import AuthRequiredError, BlurayError, PanasonicUB820
from devices.dac import ToppingD900, UnknownInputError
from devices.music import AppleMusic, MusicError, MusicKit
from devices.tv import LGTelevision, NotConnectedError, PowerOnTimeout

__all__ = [
    "AmpError",
    "AppleMusic",
    "AuthRequiredError",
    "BlasterError",
    "BlurayError",
    "ITachIR",
    "LGTelevision",
    "McIntoshMAC7200",
    "MusicError",
    "MusicKit",
    "NotConnectedError",
    "PanasonicUB820",
    "PowerOnTimeout",
    "ToppingD900",
    "UnknownInputError",
]
