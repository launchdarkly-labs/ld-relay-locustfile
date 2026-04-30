"""LaunchDarkly Locust user-emulator package.

Public surface:

- :class:`LaunchDarklyLocust` - server-side User base class
- :class:`LaunchDarklyMobileLocust` - mobile/client-side User base class
- :func:`enable_flag_propagation_metric` - opt-in flag-update timing metric
- :class:`MobileLDClient`, :class:`MobileConfig` - underlying mobile emulator
  building blocks (rarely needed directly)
"""

from .heartbeat import enable_flag_propagation_metric
from .mobile import LaunchDarklyMobileLocust, MobileConfig, MobileLDClient
from .server import LaunchDarklyLocust

__all__ = [
    "LaunchDarklyLocust",
    "LaunchDarklyMobileLocust",
    "MobileConfig",
    "MobileLDClient",
    "enable_flag_propagation_metric",
]
