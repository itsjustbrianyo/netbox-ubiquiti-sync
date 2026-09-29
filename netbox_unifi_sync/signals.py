"""
App-ready hook — ensures the custom fields exist on dcim.site the first time
the plugin loads, so users see unifi_site_id and unifi_site_name on Site forms
immediately after installation (no separate setup command).
"""

from .custom_fields import ensure_custom_field


def _ensure_site_custom_fields() -> None:
    """
    Create the two UniFi custom fields on dcim.site if they do not exist.
    Silently no-ops if the database is not yet initialised (e.g. before the
    first migration run).
    """
    ensure_custom_field(
        "dcim", "site", "unifi_site_id", "UniFi Site ID",
        "UniFi site to sync into this site: UUID, internal reference or "
        "exact name. See: manage.py sync_unifi --list-sites",
    )
    ensure_custom_field(
        "dcim", "site", "unifi_site_name", "UniFi Site Name",
        "Human-readable UniFi site name. Filled in by the sync command.",
    )


# Run once when the plugin's AppConfig.ready() fires.
_ensure_site_custom_fields()
