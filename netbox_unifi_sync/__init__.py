from netbox.plugins import PluginConfig

__version__ = "0.2.4"


class UnifiSyncConfig(PluginConfig):
    name = "netbox_unifi_sync"
    verbose_name = "UniFi Sync"
    description = "Synchronise UniFi Network (UniFi OS) devices into NetBox"
    version = __version__
    author = "NetBox UniFi Sync"
    base_url = "unifi"
    min_version = "4.2.0"

    default_settings = {
        # UniFi OS console URL, no path, e.g. "https://unifi.example.com"
        "controller_url": "",

        # Integration API key (UniFi Network -> Settings -> Integrations).
        "api_key": "",

        # OPTIONAL local, read-only UniFi OS account for the Classic API.
        # Without it the sync still runs, but serial numbers, firmware, port
        # VLAN/PoE config and static routes are unavailable.
        "username": "",
        "password": "",

        # True, False, or a path to a CA bundle file.
        "verify_ssl": True,

        # UniFi API request timeout in seconds.
        "request_timeout": 30,

        # Device role slug used when a device's family can't be determined.
        # The role is created automatically if it does not exist.
        "default_device_role": "network",

        # NetBox username that changelog entries are attributed to.
        # Created automatically as an inactive account if it doesn't exist.
        "changelog_username": "unifi-sync",

        # Enrich newly created Ubiquiti DeviceTypes (rack height, weight,
        # images...) from the NetBox Community Device Type Library on GitHub.
        "enable_devicetype_library": False,
        "devicetype_library_cache_days": 7,
    }

    def ready(self):
        super().ready()
        from . import signals  # noqa: F401 — registers the ready() hook


config = UnifiSyncConfig
