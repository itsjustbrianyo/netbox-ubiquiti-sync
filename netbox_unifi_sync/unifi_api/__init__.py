"""UniFi OS API clients. No Django imports -- usable standalone (see scripts/unifi_probe.py)."""
from .classic import ClassicClient
from .exceptions import (
    UnifiAPIError,
    UnifiAuthError,
    UnifiConnectionError,
    UnifiError,
    UnifiNotFound,
)
from .integration import IntegrationClient

__all__ = [
    "ClassicClient",
    "IntegrationClient",
    "UnifiAPIError",
    "UnifiAuthError",
    "UnifiConnectionError",
    "UnifiError",
    "UnifiNotFound",
]
