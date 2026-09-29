import django_tables2 as tables
from netbox.tables import NetBoxTable, columns

from ..models import SyncLog


class SyncLogTable(NetBoxTable):
    # Sync logs are write-once records: no edit/delete actions, no bulk select.
    pk = None
    actions = None

    unifi_site_id   = tables.Column(verbose_name="UniFi Site ID", linkify=True)
    unifi_site_name = tables.Column(verbose_name="UniFi Site Name")
    site_name       = tables.Column(verbose_name="NetBox Site")
    started_at      = tables.DateTimeColumn(verbose_name="Started")
    completed_at    = tables.DateTimeColumn(verbose_name="Completed", orderable=True)
    status          = columns.ChoiceFieldColumn(verbose_name="Status")
    devices_seen    = tables.Column(verbose_name="Devices")
    devices_created = tables.Column(verbose_name="Created")
    devices_updated = tables.Column(verbose_name="Updated")
    interfaces_synced = tables.Column(verbose_name="Interfaces")
    macs_synced       = tables.Column(verbose_name="MACs")
    vlans_synced      = tables.Column(verbose_name="VLANs")
    prefixes_synced   = tables.Column(verbose_name="Prefixes")
    static_routes_synced = tables.Column(verbose_name="Static Routes")
    wireless_lans_synced = tables.Column(verbose_name="SSIDs")

    class Meta(NetBoxTable.Meta):
        model = SyncLog
        fields = (
            "unifi_site_id",
            "unifi_site_name",
            "site_name",
            "status",
            "started_at",
            "completed_at",
            "devices_seen",
            "devices_created",
            "devices_updated",
            "interfaces_synced",
            "macs_synced",
            "vlans_synced",
            "prefixes_synced",
            "static_routes_synced",
            "wireless_lans_synced",
        )
        default_columns = (
            "unifi_site_name",
            "site_name",
            "status",
            "started_at",
            "devices_seen",
            "devices_created",
            "macs_synced",
        )
