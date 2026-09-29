"""
Plugin configuration helpers.

Everything comes from PLUGINS_CONFIG["netbox_unifi_sync"] in configuration.py —
the same place the Meraki plugin keeps its API key.  Example:

    PLUGINS_CONFIG = {
        "netbox_unifi_sync": {
            "controller_url": "https://unifi.example.com",
            "api_key": "your-integration-api-key",
            # Optional (enables serials, firmware, port VLAN/PoE, static routes):
            "username": "readonly-local-account",
            "password": "its-password",
        },
    }
"""

from netbox.plugins import get_plugin_config

from .unifi_api import ClassicClient, IntegrationClient

PLUGIN_NAME = "netbox_unifi_sync"


def plugin_setting(key: str, default=None):
    value = get_plugin_config(PLUGIN_NAME, key)
    return default if value in (None, "") else value


def parse_verify(value):
    """Accept True/False, 'true'/'false', or a CA bundle path."""
    if isinstance(value, bool):
        return value
    text = str(value).strip()
    if text.lower() in ("true", "1", "yes", ""):
        return True
    if text.lower() in ("false", "0", "no"):
        return False
    return text  # CA bundle path


def integration_client() -> IntegrationClient:
    """Integration API client.  Raises RuntimeError if not configured."""
    url = plugin_setting("controller_url", "")
    key = plugin_setting("api_key", "")
    if not url or not key:
        raise RuntimeError(
            "controller_url and api_key must be set in PLUGINS_CONFIG"
            f"[{PLUGIN_NAME!r}]."
        )
    return IntegrationClient(
        url,
        key,
        verify=parse_verify(plugin_setting("verify_ssl", True)),
        timeout=plugin_setting("request_timeout", 30),
    )


def classic_client():
    """Classic API client, or None if username/password aren't configured."""
    url = plugin_setting("controller_url", "")
    user = plugin_setting("username", "")
    password = plugin_setting("password", "")
    if not (url and user and password):
        return None
    return ClassicClient(
        url,
        user,
        password,
        verify=parse_verify(plugin_setting("verify_ssl", True)),
        timeout=plugin_setting("request_timeout", 30),
    )
