from django.db import models
from django.urls import reverse

from ..choices import SyncStatusChoices
from .querysets import PluginQuerySet


class SyncLog(models.Model):
    """
    Audit record for a single UniFi → NetBox sync run against one site.
    Plain Django model (not NetBoxModel) — sync logs are write-once records,
    not user-editable objects.
    """

    objects = PluginQuerySet.as_manager()

    unifi_site_id   = models.CharField(max_length=100, db_index=True)
    unifi_site_name = models.CharField(max_length=200, blank=True)
    site_name       = models.CharField(max_length=200, blank=True)

    started_at   = models.DateTimeField(auto_now_add=True, db_index=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    status  = models.CharField(
        max_length=20,
        choices=SyncStatusChoices,
        default=SyncStatusChoices.PENDING,
        db_index=True,
    )
    message = models.TextField(blank=True)

    # Counters
    devices_seen         = models.PositiveIntegerField(default=0)
    devices_created      = models.PositiveIntegerField(default=0)
    devices_updated      = models.PositiveIntegerField(default=0)
    interfaces_synced    = models.PositiveIntegerField(default=0)
    macs_synced          = models.PositiveIntegerField(default=0)
    ips_synced           = models.PositiveIntegerField(default=0)
    vlans_synced         = models.PositiveIntegerField(default=0)
    prefixes_synced      = models.PositiveIntegerField(default=0)
    static_routes_synced = models.PositiveIntegerField(default=0)
    wireless_lans_synced = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-started_at"]
        verbose_name = "Sync Log"
        verbose_name_plural = "Sync Logs"

    def __str__(self) -> str:
        name = self.unifi_site_name or self.unifi_site_id
        return f"{name} @ {self.started_at:%Y-%m-%d %H:%M}"

    def get_absolute_url(self) -> str:
        return reverse("plugins:netbox_unifi_sync:synclog", args=[self.pk])

    @property
    def duration(self):
        if self.completed_at and self.started_at:
            return self.completed_at - self.started_at
        return None

    def get_status_color(self) -> str:
        return SyncStatusChoices.colors.get(self.status, "secondary")
