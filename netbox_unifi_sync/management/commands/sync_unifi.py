"""
Management command: sync_unifi

Syncs UniFi Network (UniFi OS) devices into NetBox by reading every NetBox Site
that has the unifi_site_id custom field populated, collecting device, IPAM and
wireless data from the corresponding UniFi site, and writing it to NetBox via
the Django ORM.

Usage
-----
  # Sync all mapped sites
  python manage.py sync_unifi

  # Sync a single UniFi site (by the value stored in unifi_site_id)
  python manage.py sync_unifi --site clqd72kz

  # Dry run — show what would change without writing anything
  python manage.py sync_unifi --dry-run

  # List all UniFi sites on the controller and exit
  python manage.py sync_unifi --list-sites

Configuration
-------------
Set the controller URL and credentials in NetBox's configuration.py:

  PLUGINS_CONFIG = {
      "netbox_unifi_sync": {
          "controller_url": "https://unifi.example.com",
          "api_key": "your-integration-api-key",
          # Optional — enables serials, firmware, port VLAN/PoE, static routes:
          "username": "readonly-local-account",
          "password": "its-password",
      }
  }

Mapping sites
-------------
Set the "UniFi Site ID" custom field on a NetBox Site to the UniFi site's UUID,
internal reference or exact name (see --list-sites).

Scheduling
----------
  # /etc/cron.d/netbox-unifi-sync
  0 */4 * * * netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py \\
      sync_unifi >> /var/log/netbox/unifi_sync.log 2>&1
"""

from __future__ import annotations

import logging
import sys

from django.core.management.base import BaseCommand

from ...change_logging import change_logging, DEFAULT_USERNAME
from ...choices import SyncStatusChoices
from ...collector import UnifiCollector
from ...models import SyncLog
from ...settings import classic_client, integration_client, plugin_setting
from ...signals import _ensure_site_custom_fields
from ...syncer import UnifiSyncer, get_mapped_sites, update_site_unifi_name

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Sync UniFi Network devices into NetBox."

    def add_arguments(self, parser):
        parser.add_argument(
            "--site",
            metavar="UNIFI_SITE",
            default=None,
            help=(
                "Sync only the NetBox site whose unifi_site_id equals this "
                "value (UUID, internal reference or name).  Default: sync all "
                "mapped sites."
            ),
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Collect data but do not write anything to NetBox.",
        )
        parser.add_argument(
            "--user",
            default=None,
            help=(
                "NetBox username that changes are attributed to in the "
                "changelog.  Default: plugin setting changelog_username, "
                f"or '{DEFAULT_USERNAME}' (created automatically, inactive)."
            ),
        )
        parser.add_argument(
            "--list-sites",
            action="store_true",
            help=(
                "List all UniFi sites on the configured controller and exit.  "
                "Useful for finding the values to put in unifi_site_id."
            ),
        )
        parser.add_argument(
            "--backfill-device-types",
            action="store_true",
            help=(
                "Enrich existing Ubiquiti DeviceTypes from the NetBox Community "
                "Device Type Library and exit, without running a sync. Only "
                "fills DeviceTypes that don't already have data (rack height, "
                "an image, or comments) — never overwrites anything. Requires "
                "enable_devicetype_library to be set in PLUGINS_CONFIG."
            ),
        )

    def _handle_backfill_device_types(self):
        if not plugin_setting("enable_devicetype_library", False):
            self.stderr.write(self.style.ERROR(
                "enable_devicetype_library is not set in PLUGINS_CONFIG. "
                "Enable it before running --backfill-device-types."
            ))
            sys.exit(1)

        syncer = UnifiSyncer(
            sync_log=SyncLog(),
            enable_devicetype_library=True,
            devicetype_library_cache_days=plugin_setting("devicetype_library_cache_days", 7),
        )
        self.stdout.write("Checking existing Ubiquiti device types against the Device Type Library...")
        enriched, skipped = syncer.backfill_device_types()
        self.stdout.write(self.style.SUCCESS(
            f"Done — {enriched} device type(s) enriched, {skipped} already had data or no match found."
        ))

    def handle(self, *args, **options):
        if options["backfill_device_types"]:
            self._handle_backfill_device_types()
            return

        try:
            integration = integration_client()
        except RuntimeError as exc:
            self.stderr.write(self.style.ERROR(
                f"{exc}\nSet controller_url and api_key in "
                "PLUGINS_CONFIG['netbox_unifi_sync'] in configuration.py."
            ))
            sys.exit(1)

        classic = classic_client()
        try:
            self._run(integration, classic, options)
        finally:
            for client in (classic, integration):
                if client is not None:
                    try:
                        client.close()
                    except Exception:  # never mask the real error
                        pass

    def _run(self, integration, classic, options):
        dry_run     = options["dry_run"]
        site_filter = options["site"]
        role_slug   = plugin_setting("default_device_role", "network")

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no database writes"))

        collector = UnifiCollector(integration, classic)

        # --list-sites mode
        if options["list_sites"]:
            self._list_sites(collector)
            return

        if classic is None:
            self.stdout.write(self.style.WARNING(
                "No username/password configured — running without the Classic API.\n"
                "Serial numbers, firmware, port VLAN/PoE settings and static routes "
                "will not be synced."
            ))

        # Ensure custom fields exist before any ORM reads
        _ensure_site_custom_fields()

        mapped_sites = get_mapped_sites()
        if not mapped_sites:
            self.stdout.write(self.style.WARNING(
                "No NetBox sites have unifi_site_id set.\n"
                "Populate the custom field on a Site to map it to a UniFi site.\n"
                "Use --list-sites to see available sites."
            ))
            return

        if site_filter:
            wanted = site_filter.strip().lower()
            mapped_sites = [
                s for s in mapped_sites
                if str(s.custom_field_data.get("unifi_site_id")).strip().lower() == wanted
            ]
            if not mapped_sites:
                self.stderr.write(self.style.ERROR(
                    f"No NetBox site has unifi_site_id = {site_filter!r}"
                ))
                sys.exit(1)

        try:
            unifi_sites = collector.list_sites()
        except Exception as exc:
            self.stderr.write(self.style.ERROR(f"Could not list UniFi sites: {exc}"))
            sys.exit(1)

        self.stdout.write(f"Syncing {len(mapped_sites)} UniFi site(s) into NetBox…")

        total_devices = total_created = total_updated = total_interfaces = 0
        total_ips = total_vlans = total_prefixes = total_static_routes = 0
        total_wireless_lans = failed_sites = 0

        changelog_user = (
            options.get("user")
            or plugin_setting("changelog_username", DEFAULT_USERNAME)
            or DEFAULT_USERNAME
        )
        # All writes below run inside NetBox's change-tracking context so they
        # appear in the changelog (one request ID per sync run).
        with change_logging(changelog_user, enabled=not dry_run):
            for site in mapped_sites:
                mapped_value = str(site.custom_field_data["unifi_site_id"]).strip()
                site_name = site.name

                self.stdout.write(f"\n[{mapped_value}] → site: {site_name}")

                meta = collector.resolve_site(mapped_value, unifi_sites)

                sync_log = SyncLog(
                    unifi_site_id   = meta["id"] if meta else mapped_value,
                    unifi_site_name = (meta or {}).get("name", ""),
                    site_name       = site_name,
                    status          = SyncStatusChoices.RUNNING,
                )
                if not dry_run:
                    sync_log.save()

                syncer = UnifiSyncer(
                    sync_log          = sync_log,
                    dry_run           = dry_run,
                    default_role_slug = role_slug,
                    enable_devicetype_library = plugin_setting("enable_devicetype_library", False),
                    devicetype_library_cache_days = plugin_setting("devicetype_library_cache_days", 7),
                )

                try:
                    if meta is None:
                        raise LookupError(
                            f"UniFi site {mapped_value!r} not found on the controller "
                            "(use --list-sites to see valid values)"
                        )

                    site_data = collector.collect_site_meta(meta)
                    self.stdout.write(
                        f"  Collected {len(site_data.devices)} device(s), "
                        f"{len(site_data.networks)} network(s), "
                        f"{len(site_data.routes)} static route(s) and "
                        f"{len(site_data.wlans)} SSID(s) from UniFi"
                    )

                    if not dry_run:
                        update_site_unifi_name(site, site_data.name)

                    # VLANs first so port VLAN assignments resolve on the first run
                    ipam_errors = syncer.sync_ipam(site_data, site=site)
                    for err in ipam_errors:
                        self.stderr.write(self.style.ERROR(f"  IPAM error: {err}"))

                    device_errors = syncer.sync_devices(site_data.devices, site=site)
                    for err in device_errors:
                        self.stderr.write(self.style.ERROR(f"  Device error: {err}"))

                    wireless_errors = syncer.sync_wireless(site_data.wlans, site=site)
                    for err in wireless_errors:
                        self.stderr.write(self.style.ERROR(f"  Wireless error: {err}"))

                    syncer.close(success=True)

                    total_devices    += sync_log.devices_seen
                    total_created    += sync_log.devices_created
                    total_updated    += sync_log.devices_updated
                    total_interfaces += sync_log.interfaces_synced
                    total_ips        += sync_log.ips_synced
                    total_vlans      += sync_log.vlans_synced
                    total_prefixes   += sync_log.prefixes_synced
                    total_static_routes += sync_log.static_routes_synced
                    total_wireless_lans += sync_log.wireless_lans_synced

                    self.stdout.write(self.style.SUCCESS(
                        f"  Done — "
                        f"{sync_log.devices_created} created, "
                        f"{sync_log.devices_updated} updated, "
                        f"{sync_log.interfaces_synced} interfaces, "
                        f"{sync_log.ips_synced} IPs, "
                        f"{sync_log.vlans_synced} VLANs, "
                        f"{sync_log.prefixes_synced} prefixes, "
                        f"{sync_log.static_routes_synced} static routes"
                        + (f", {sync_log.wireless_lans_synced} SSIDs" if sync_log.wireless_lans_synced else "")
                    ))

                except Exception as exc:
                    if isinstance(exc, LookupError):
                        logger.error("Sync failed for UniFi site %s: %s", mapped_value, exc)
                    else:
                        logger.exception("Sync failed for UniFi site %s: %s", mapped_value, exc)
                    syncer.close(success=False, message=str(exc))
                    self.stderr.write(self.style.ERROR(f"  ERROR: {exc}"))
                    failed_sites += 1

        # Summary
        self.stdout.write("")
        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"Dry-run complete. Would have synced {total_devices} device(s) "
                f"across {len(mapped_sites)} site(s)."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"Sync complete — "
                f"{total_devices} devices seen, "
                f"{total_created} created, "
                f"{total_updated} updated, "
                f"{total_interfaces} interfaces, "
                f"{total_ips} IPs, "
                f"{total_vlans} VLANs, {total_prefixes} prefixes, "
                f"{total_static_routes} static routes"
                + (f", {total_wireless_lans} SSIDs" if total_wireless_lans else "")
                + (f"  [{failed_sites} site(s) failed]" if failed_sites else "")
            ))

    # ------------------------------------------------------------------
    # List sites helper
    # ------------------------------------------------------------------

    def _list_sites(self, collector: UnifiCollector) -> None:
        try:
            sites = collector.list_sites()
        except Exception as exc:
            self.stderr.write(self.style.ERROR(f"Could not list UniFi sites: {exc}"))
            return
        if not sites:
            self.stderr.write(self.style.ERROR(
                "No sites returned.  Check the API key has access."
            ))
            return

        self.stdout.write(f"\nUniFi sites ({len(sites)}):")
        self.stdout.write("-" * 78)
        for s in sites:
            self.stdout.write(
                f"  {s.get('id', '?'):<38}  {s.get('internalReference', '?'):<10}  {s.get('name', '?')}"
            )
