"""
NetBox ORM syncer.

Takes the Collected* objects from collector.py and writes them to NetBox using
the Django ORM.  Structure and behaviour deliberately mirror the Meraki plugin's
syncer: one SyncLog per site, per-item atomic transactions so a single failure
does not abort the whole site, changelog-friendly snapshots before updates.

NetBox objects managed
-----------------------
dcim.Manufacturer         — "Ubiquiti" (created once, reused)
dcim.DeviceType           — per UniFi model string
dcim.DeviceRole           — Switch / Firewall / Access Point (or the plugin default_device_role)
dcim.Device               — one per UniFi device; matched by unifi_mac custom field, then serial, then site+name
dcim.Interface            — one per switch/gateway port (+ "wifi0" radio on access points)
dcim.MACAddress           — the device base MAC, linked to its first interface
ipam.IPAddress            — device management IPs, gateway WAN IPs (/32) and per-network gateway IPs
ipam.VLAN                 — one per UniFi network that has a VLAN ID
ipam.VLANGroup            — one per site ("{site} - UniFi - VLANs"), scoping VLAN IDs
ipam.VRF                  — one per site ("{site} - UniFi - VRF") holding this site's prefixes/ranges/IPs
ipam.Prefix               — one per network subnet and per enabled static route
ipam.IPRange              — one per network/static-route subnet, spanning usable host addresses
extras.Tag                — "unifi" on everything this plugin creates, plus tags inherited from the Site
wireless.WirelessLAN      — one per enabled SSID, in a per-site WirelessLANGroup ("{site} - UniFi - WLANs")

Custom fields
-------------
  dcim.site       unifi_site_id, unifi_site_name        (created on plugin load — see signals.py)
  dcim.device     unifi_mac, unifi_device_id, unifi_firmware, unifi_uplink_config
  dcim.interface  unifi_poe_enabled, unifi_poe_mode

Coexistence with netbox-meraki-sync
-----------------------------------
Sites can hold both vendors' gear.  The per-site VRF, VLANGroup and
WirelessLANGroup all have "UniFi" in their names, so this plugin's VLANs,
prefixes, IP ranges and SSIDs live in their own containers and never mix with
Meraki's.  For objects found outside those containers (created by hand, say)
this plugin only overwrites ones carrying its own ``unifi`` tag; anything
else is only gap-filled (blank fields) and never re-tagged or renamed.  Radio
interfaces are named "wifi0" (Meraki uses "Radio 0") so SSIDs are never attached
to the other vendor's access points, and devices are never matched by name if
they are tagged ``meraki``.
"""

from __future__ import annotations

import ipaddress
import logging
from datetime import datetime, timezone
from typing import Optional

from django.db import transaction
from django.utils.text import slugify

from .custom_fields import ensure_custom_field
from .dataclasses import (
    CollectedDevice, CollectedInterface, CollectedNetwork,
    CollectedRoute, CollectedSite, CollectedWlan,
)

log = logging.getLogger(__name__)

# Interface type by device family (refined per-port by link speed below)
_FAMILY_IFACE_TYPE = {
    "switch":  "1000base-t",
    "gateway": "1000base-t",
    "ap":      "1000base-t",   # the AP's wired uplink; its radio is typed separately
}

_SPEED_TO_IFACE_TYPE = {         # keyed on Mbps
    10:     "10base-t",
    100:    "100base-tx",
    1000:   "1000base-t",
    2500:   "2.5gbase-t",
    5000:   "5gbase-t",
    10000:  "10gbase-t",
    25000:  "25gbase-x-sfp28",
    40000:  "40gbase-x-qsfpp",
    100000: "100gbase-x-qsfp28",
}

_UBIQUITI_MANUFACTURER = "Ubiquiti"
_UNIFI_TAG_SLUG  = "unifi"
_UNIFI_TAG_NAME  = "unifi"
_UNIFI_TAG_COLOR = "0559c9"
_LEGACY_TAG_SLUG = "src-unifi"      # 0.2.0 named the tag "src:unifi"; renamed in place on first use
_MERAKI_TAG_SLUG = "meraki"

_RADIO_IFACE_NAME = "wifi0"


# Per-site container objects.  Every name carries "UniFi" so they can never
# collide with the ones netbox-meraki-sync creates ("{site} VRF", "{site} VLANs",
# "{site} WLANs") on a site that has both vendors' gear.
def vrf_name(site) -> str:
    return f"{site.name} - UniFi - VRF"


def vlan_group_name(site) -> str:
    return f"{site.name} - UniFi - VLANs"


def wlan_group_name(site) -> str:
    return f"{site.name} - UniFi - WLANs"

# Networks whose purpose contains one of these aren't LAN subnets worth modelling.
_SKIP_NETWORK_PURPOSES = ("wan", "vpn")


class UnifiSyncer:
    """
    Sync collected UniFi data into NetBox.

    Usage::

        syncer = UnifiSyncer(sync_log=log_obj, dry_run=False)
        syncer.sync_ipam(site_data, site=nb_site)
        syncer.sync_devices(site_data.devices, site=nb_site)
        syncer.sync_wireless(site_data.wlans, site=nb_site)
        syncer.close()   # finalises the SyncLog record
    """

    def __init__(
        self,
        sync_log,
        *,
        dry_run: bool = False,
        sync_ips: bool = True,
        default_role_slug: str = "network",
        enable_devicetype_library: bool = False,
        devicetype_library_cache_days: int = 7,
    ) -> None:
        self.log               = sync_log
        self.dry_run           = dry_run
        self.sync_ips          = sync_ips
        self.default_role_slug = default_role_slug
        self.enable_devicetype_library = enable_devicetype_library

        # Cached lookups populated on first use
        self._manufacturer  = None
        self._unifi_tag     = None
        self._role_cache: dict[str, object]        = {}
        self._device_type_cache: dict[str, object] = {}
        self._devicetype_library = None
        if enable_devicetype_library:
            from .devicetype_library import DeviceTypeLibrary
            self._devicetype_library = DeviceTypeLibrary(cache_days=devicetype_library_cache_days)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def sync_devices(self, devices: list[CollectedDevice], site) -> list[str]:
        """
        Sync a list of CollectedDevice objects into NetBox under the given site.
        Updates self.log counters in place.  Returns human-readable errors for
        any device that failed, so the caller can print them.
        """
        errors: list[str] = []
        self.log.devices_seen = len(devices)
        self._get_unifi_tag()

        for dev in devices:
            try:
                with transaction.atomic():
                    self._sync_device(dev, site)
            except Exception as exc:
                log.warning("Syncer: failed to sync device %s (%s): %s", dev.mac, dev.name, exc)
                if "unique_name_site_tenant" in str(exc):
                    errors.append(
                        f"Device {dev.name!r} ({dev.mac}): a device with this name already "
                        "exists at this site (added by hand or by another sync, e.g. Meraki). "
                        "Rename one of them and the next run will pick it up."
                    )
                else:
                    errors.append(f"Device {dev.name!r} ({dev.mac}): {exc!r}")

        if not self.dry_run:
            self.log.save(update_fields=[
                "devices_seen", "devices_created", "devices_updated",
                "interfaces_synced", "macs_synced", "ips_synced",
            ])
        return errors

    def sync_ipam(self, site_data: CollectedSite, site) -> list[str]:
        """
        Sync a site's networks (VLAN + Prefix + IP range + gateway IP) and
        enabled static routes into NetBox IPAM, scoped to the given site.
        VLANGroups, VLANs, Prefixes, IP ranges and WirelessLANs inherit the
        site's tenant and tags.  Everything is scoped to a per-site VRF named
        '{site_name} - UniFi - VRF'.

        Returns human-readable errors for any network or route that failed.
        """
        errors: list[str] = []
        if site is None:
            return errors

        self._get_unifi_tag()   # rename a legacy tag first: ownership checks below rely on it
        vlan_group = self._get_vlan_group(site)
        vrf = self._get_or_create_vrf(site)

        for cn in site_data.networks:
            if not self._network_is_syncable(cn):
                continue
            try:
                with transaction.atomic():
                    nb_vlan = self._sync_vlan(cn, site, vlan_group, vrf)
                    self._sync_prefix(cn, site, nb_vlan, vrf)
                    self._sync_vlan_ip(cn, site, vrf)
                    self._sync_vlan_ip_range(cn, site, vrf)
            except Exception as exc:
                log.exception(
                    "Syncer: failed to sync network %s (%s) for site %s",
                    cn.vlan_id, cn.name, site,
                )
                errors.append(f"Network {cn.name!r} (VLAN {cn.vlan_id}): {exc!r}")

        for route in site_data.routes:
            try:
                with transaction.atomic():
                    self._sync_static_route(route, site, vrf)
            except Exception as exc:
                log.exception(
                    "Syncer: failed to sync static route %s (%s) for site %s",
                    route.name, route.network, site,
                )
                errors.append(f"Static route {route.name!r} ({route.network}): {exc!r}")

        if not self.dry_run:
            self.log.save(update_fields=[
                "vlans_synced", "prefixes_synced", "static_routes_synced", "ips_synced",
            ])
        return errors

    def sync_wireless(self, wlans: list[CollectedWlan], site) -> list[str]:
        """
        Sync a site's enabled SSIDs into NetBox as wireless.WirelessLAN
        records, scoped to a per-site WirelessLANGroup (so the same SSID name
        reused at different schools doesn't collide), inheriting the site's
        tenant + tags.  Each synced WLAN is also attached to the "wifi0" radio
        of every UniFi access point already synced at this site, since UniFi
        SSIDs are broadcast site-wide.

        Returns human-readable errors the same way sync_ipam does.
        """
        errors: list[str] = []
        wlans = [w for w in wlans if w.enabled and w.ssid]
        if not wlans or site is None:
            return errors

        self._get_unifi_tag()
        wlan_group = self._get_wireless_lan_group(site)
        synced_wlans = []

        for cw in wlans:
            try:
                with transaction.atomic():
                    wlan = self._sync_wireless_lan(cw, site, wlan_group)
                    if wlan is not None:
                        synced_wlans.append(wlan)
            except Exception as exc:
                log.exception("Syncer: failed to sync SSID %s for site %s", cw.ssid, site)
                errors.append(f"SSID {cw.ssid!r}: {exc!r}")

        if synced_wlans and not self.dry_run:
            try:
                self._attach_wireless_lans_to_aps(synced_wlans, site)
            except Exception as exc:
                log.exception("Syncer: failed to attach SSIDs to APs for site %s", site)
                errors.append(f"Attaching SSIDs to APs: {exc!r}")

        if not self.dry_run:
            self.log.save(update_fields=["wireless_lans_synced"])
        return errors

    def close(self, *, success: bool = True, message: str = "") -> None:
        """Stamp the SyncLog with a completion time and status."""
        from .choices import SyncStatusChoices
        self.log.completed_at = datetime.now(tz=timezone.utc)
        self.log.status = SyncStatusChoices.SUCCESS if success else SyncStatusChoices.FAILED
        self.log.message = message
        if not self.dry_run:
            self.log.save()

    # ------------------------------------------------------------------
    # Per-device sync
    # ------------------------------------------------------------------

    def _find_device(self, dev: CollectedDevice, site):
        """
        Match an existing NetBox device: unifi_mac custom field first, then
        serial, then site + name.  The name fallback never picks up a device
        that is tagged 'meraki' or already bound to a different UniFi MAC, so
        two vendors' gear that share a hostname on a site can't be confused.
        """
        from dcim.models import Device

        mac = (dev.mac or "").lower()
        device = None
        if mac:
            device = Device.objects.filter(custom_field_data__unifi_mac=mac).first()
        if device is None and dev.serial:
            device = Device.objects.filter(serial=dev.serial).first()
        if device is None and site is not None:
            for cand in Device.objects.filter(site=site, name=dev.name).exclude(
                tags__slug=_MERAKI_TAG_SLUG
            ):
                bound = (cand.custom_field_data or {}).get("unifi_mac")
                if not bound or bound.lower() == mac:
                    device = cand
                    break
        return device

    def _sync_device(self, dev: CollectedDevice, site) -> None:
        from dcim.models import Device

        if self.dry_run:
            # A dry run must not write anything — not even the Manufacturer,
            # DeviceType, role or tag — so just count what would happen.
            if self._find_device(dev, site) is None:
                self.log.devices_created += 1
            self.log.interfaces_synced += len(dev.interfaces)
            return

        manufacturer = self._get_manufacturer()
        device_type  = self._get_device_type(dev.model, manufacturer)
        role         = self._get_role(dev.family)
        tag          = self._get_unifi_tag()
        tenant       = site.tenant if site else None

        device = self._find_device(dev, site)

        if device is None:
            device = Device.objects.create(
                name        = dev.name,
                serial      = dev.serial or "",
                device_type = device_type,
                role        = role,
                site        = site,
                tenant      = tenant,  # Inherits Tenant from parent Site
                status      = "active",
                latitude    = site.latitude if site else None,
                longitude   = site.longitude if site else None,
            )
            self.log.devices_created += 1
            log.info("Syncer: created device %s (%s)", dev.name, dev.mac)
        else:
            if hasattr(device, "snapshot"):
                device.snapshot()  # pre-change state for changelog diff
            changed = []
            if device.name != dev.name:
                device.name = dev.name
                changed.append("name")
            # Only overwrite the serial when the Classic API supplied one
            if dev.serial and device.serial != dev.serial:
                device.serial = dev.serial
                changed.append("serial")
            if device.device_type_id != device_type.pk:
                device.device_type = device_type
                changed.append("device_type")
            if device.site_id != site.pk:
                device.site = site
                changed.append("site")
            if device.tenant_id != (tenant.pk if tenant else None):
                device.tenant = tenant
                changed.append("tenant")
            if device.status != "active":
                device.status = "active"
                changed.append("status")

            # Sync GPS coordinates from parent Site
            site_lat = site.latitude if site else None
            site_lon = site.longitude if site else None
            if device.latitude != site_lat:
                device.latitude = site_lat
                changed.append("latitude")
            if device.longitude != site_lon:
                device.longitude = site_lon
                changed.append("longitude")

            if changed:
                device.save(update_fields=changed)
                self.log.devices_updated += 1
                log.debug("Syncer: updated device %s (%s) — %s", dev.name, dev.mac, ", ".join(changed))

        # Apply the "unifi" tag as well as all tags assigned to the parent Site
        tags_to_add = []
        if tag:
            tags_to_add.append(tag)
        if site and site.tags.exists():
            tags_to_add.extend(list(site.tags.all()))
        if tags_to_add:
            device.tags.add(*tags_to_add)

        # Custom fields: MAC, UniFi device ID, firmware, uplink config
        self._sync_device_custom_fields(device, dev)

        # Interfaces
        synced: list[tuple[CollectedInterface, object]] = []
        for port in dev.interfaces:
            iface = self._sync_interface(device, port, dev.family)
            if iface is not None:
                synced.append((port, iface))

        # Access points get a radio interface so SSIDs can be attached to it
        radio = None
        if dev.family == "ap":
            radio = self._ensure_radio_interface(device, dev.model)

        # The management interface: first non-WAN port, else the radio
        mgmt_iface = next((i for p, i in synced if not self._is_wan_name(p.name)), None) or radio

        # Base MAC on the management interface
        if mgmt_iface is not None:
            self._sync_mac(mgmt_iface, dev.mac)

        vrf = self._get_or_create_vrf(site) if self.sync_ips else None

        # Management IP on the management interface
        if self.sync_ips and dev.management_ip and mgmt_iface is not None:
            self._sync_ip(
                mgmt_iface, dev.management_ip, dev.management_prefix_length,
                site=site, vrf=vrf,
            )

        # Gateway WAN IPs — ISP-assigned public addresses, not part of a LAN
        # subnet UniFi manages, so always synced as a standalone /32.
        wan_ips: list[str] = []
        if self.sync_ips:
            for port, iface in synced:
                if port.ip_address and self._is_wan_name(port.name):
                    self._sync_ip(iface, port.ip_address, prefix_length=32, site=site, vrf=vrf)
                    wan_ips.append(port.ip_address)

        # Primary IPv4: prefer the management IP, then WAN1, then WAN2
        if self.sync_ips:
            primary_candidate = dev.management_ip or (wan_ips[0] if wan_ips else None)
            if primary_candidate:
                self._set_primary_ip4(device, primary_candidate)

    @staticmethod
    def _is_wan_name(name: str) -> bool:
        return (name or "").upper().startswith("WAN")

    # ------------------------------------------------------------------
    # Primary IP assignment
    # ------------------------------------------------------------------

    def _set_primary_ip4(self, device, ip_str: str) -> None:
        """
        Point device.primary_ip4 at the IPAddress matching ip_str that is
        assigned to one of this device's interfaces (created earlier in
        _sync_device via _sync_ip).  No-ops if no matching, interface-
        assigned IPAddress exists yet.
        """
        from ipam.models import IPAddress as IPAddr
        from dcim.models import Interface
        from django.contrib.contenttypes.models import ContentType

        iface_ids = list(Interface.objects.filter(device=device).values_list("pk", flat=True))
        if not iface_ids:
            return

        iface_ct = ContentType.objects.get_for_model(Interface)
        candidates = IPAddr.objects.filter(
            assigned_object_type=iface_ct,
            assigned_object_id__in=iface_ids,
        )
        match = next(
            (ip for ip in candidates if str(ip.address).split("/", 1)[0] == ip_str),
            None,
        )
        if match is None:
            return

        if device.primary_ip4_id != match.pk:
            if hasattr(device, "snapshot"):
                device.snapshot()
            device.primary_ip4 = match
            device.save(update_fields=["primary_ip4"])
            log.debug("Syncer: set primary IPv4 %s on device %s", ip_str, device.name)

    # ------------------------------------------------------------------
    # Interface sync
    # ------------------------------------------------------------------

    def _sync_interface(self, device, port: CollectedInterface, family: str):
        from dcim.models import Interface

        speed_mbps = (port.speed // 1000) if port.speed else None
        iface_type = self._iface_type(family, speed_mbps)
        nb_duplex  = port.duplex or "auto"
        nb_mode    = port.mode or None
        want_untagged = (
            self._get_vlan_by_id(port.untagged_vlan_vid, device.site)
            if nb_mode and port.untagged_vlan_vid else None
        )
        want_tagged = (
            self._get_vlans_by_ids(port.tagged_vlan_vids, device.site)
            if nb_mode == "tagged" and port.tagged_vlan_vids else []
        )

        iface = Interface.objects.filter(device=device, name=port.name).first()

        if iface is None:
            iface = Interface.objects.create(
                device        = device,
                name          = port.name,
                type          = iface_type,
                enabled       = port.enabled,
                speed         = port.speed or None,   # NetBox stores kbps
                duplex        = nb_duplex,
                mode          = nb_mode,
                untagged_vlan = want_untagged,
            )
            if want_tagged:
                iface.tagged_vlans.set(want_tagged)
            self.log.interfaces_synced += 1
        else:
            if hasattr(iface, "snapshot"):
                iface.snapshot()  # pre-change state for changelog diff
            changed = []
            if iface.enabled != port.enabled:
                iface.enabled = port.enabled
                changed.append("enabled")

            nb_speed = port.speed or None
            if iface.speed != nb_speed:
                iface.speed = nb_speed
                changed.append("speed")
            if iface.duplex != nb_duplex:
                iface.duplex = nb_duplex
                changed.append("duplex")
            if (iface.mode or None) != nb_mode:
                iface.mode = nb_mode
                changed.append("mode")
            if iface.untagged_vlan_id != (want_untagged.pk if want_untagged else None):
                iface.untagged_vlan = want_untagged
                changed.append("untagged_vlan")

            if changed:
                iface.save(update_fields=changed)

            # Tagged VLANs (M2M) are a separate write path
            tagged_changed = False
            existing = {v.pk for v in iface.tagged_vlans.all()}
            wanted = {v.pk for v in want_tagged}
            if existing != wanted:
                tagged_changed = True
                iface.tagged_vlans.set(want_tagged)

            if changed or tagged_changed:
                self.log.interfaces_synced += 1
                log.debug(
                    "Syncer: updated interface %s on %s — %s",
                    port.name, device.name,
                    ", ".join(changed + (["tagged_vlans"] if tagged_changed else [])),
                )

        self._sync_interface_poe_fields(iface, port)
        return iface

    def _ensure_radio_interface(self, device, model: str):
        """Get-or-create the access point's radio interface (named 'wifi0')."""
        from dcim.models import Interface

        iface = Interface.objects.filter(device=device, name=_RADIO_IFACE_NAME).first()
        if iface is None:
            iface = Interface.objects.create(
                device=device,
                name=_RADIO_IFACE_NAME,
                type=self._radio_iface_type(model),
                enabled=True,
            )
            self.log.interfaces_synced += 1
        return iface

    @staticmethod
    def _radio_iface_type(model: str) -> str:
        m = (model or "").upper().replace(" ", "").replace("-", "")
        if m.startswith(("U7", "E7", "UWB7")):
            return "ieee802.11be"
        if m.startswith(("UAP", "UAL", "UAC", "UDM", "UBB")):
            return "ieee802.11ac"
        return "ieee802.11ax"   # U6 family and unknown models

    # ------------------------------------------------------------------
    # MAC address sync
    # ------------------------------------------------------------------

    def _sync_mac(self, iface, mac_str: str) -> None:
        """
        Create a MACAddress record linked to the given interface if it does
        not already exist.  Existing MACs are not modified (they may be
        managed by another tool or have analyst notes).
        """
        from dcim.models import MACAddress
        from django.contrib.contenttypes.models import ContentType

        if not mac_str or self.dry_run:
            return

        iface_ct = ContentType.objects.get_for_model(iface)
        if not MACAddress.objects.filter(mac_address=mac_str).exists():
            try:
                with transaction.atomic():
                    MACAddress.objects.create(
                        mac_address          = mac_str,
                        assigned_object_type = iface_ct,
                        assigned_object_id   = iface.pk,
                    )
                self.log.macs_synced += 1
            except Exception as exc:
                log.debug("Syncer: could not create MAC %s: %s", mac_str, exc)

    # ------------------------------------------------------------------
    # IP address sync
    # ------------------------------------------------------------------

    def _sync_ip(self, iface, ip_str: str, prefix_length: int = 24, *, site=None, vrf=None) -> None:
        """
        Create an IPAddress record linked to the interface if it does not
        exist.  Looked up by host address alone within the site's VRF (not the
        full address/mask string), so if a previous sync recorded this host
        with a different mask the existing record's mask is corrected in place
        instead of leaving the old one behind and creating a duplicate.
        Scoping the lookup to the site's VRF matters for UniFi: many sites use
        the same default private ranges, which must not collide.

        Inherits tenant + tags from `site` the same way every other synced
        IPAM object does, when a site is supplied.
        """
        from ipam.models import IPAddress as IPAddr
        from django.contrib.contenttypes.models import ContentType

        if not ip_str or self.dry_run:
            return

        address = f"{ip_str}/{prefix_length}"
        iface_ct = ContentType.objects.get_for_model(iface)

        ip_obj = IPAddr.objects.filter(address__net_host=ip_str, vrf=vrf).first()
        if ip_obj is None:
            try:
                with transaction.atomic():
                    ip_obj = IPAddr.objects.create(
                        address              = address,
                        status               = "active",
                        vrf                  = vrf,
                        assigned_object_type = iface_ct,
                        assigned_object_id   = iface.pk,
                    )
                self.log.ips_synced += 1
            except Exception as exc:
                log.debug("Syncer: could not create IP %s: %s", address, exc)
                return
        else:
            if hasattr(ip_obj, "snapshot"):
                ip_obj.snapshot()  # pre-change state for changelog diff
            changed = []
            if str(ip_obj.address) != address:
                ip_obj.address = address
                changed.append("address")
            # Assign to this interface if currently unassigned
            if ip_obj.assigned_object_id is None:
                ip_obj.assigned_object_type = iface_ct
                ip_obj.assigned_object_id   = iface.pk
                changed.extend(["assigned_object_type", "assigned_object_id"])
            if changed:
                ip_obj.save(update_fields=changed)

        if site is not None:
            self._apply_site_scope(ip_obj, site)

    # ------------------------------------------------------------------
    # Custom fields on Device / Interface
    # ------------------------------------------------------------------

    def _sync_device_custom_fields(self, device, dev: CollectedDevice) -> None:
        """Write UniFi-specific data into custom fields on the Device record."""
        if self.dry_run:
            return

        wanted: dict[str, tuple[str, object]] = {}
        if dev.mac:
            wanted["unifi_mac"] = ("UniFi MAC", dev.mac.lower())
        if dev.unifi_device_id:
            wanted["unifi_device_id"] = ("UniFi Device ID", dev.unifi_device_id)
        if dev.firmware:
            wanted["unifi_firmware"] = ("UniFi Firmware", dev.firmware)
        if dev.uplink_config:
            wanted["unifi_uplink_config"] = ("UniFi Uplink Config", dev.uplink_config)

        data = dict(device.custom_field_data or {})
        updates = {}
        for name, (label, value) in wanted.items():
            self._ensure_device_cf(name, label)
            if data.get(name) != value:
                updates[name] = value

        if updates:
            if hasattr(device, "snapshot"):
                device.snapshot()
            data.update(updates)
            device.custom_field_data = data
            device.save(update_fields=["custom_field_data"])

    def _sync_interface_poe_fields(self, iface, port: CollectedInterface) -> None:
        """Write PoE configuration to custom fields on the Interface record."""
        if self.dry_run or not iface or port.poe_enabled is None:
            return

        self._ensure_interface_poe_cf()
        data = dict(iface.custom_field_data or {})
        updates = {}
        if data.get("unifi_poe_enabled") != bool(port.poe_enabled):
            updates["unifi_poe_enabled"] = bool(port.poe_enabled)
        if port.poe_mode and data.get("unifi_poe_mode") != port.poe_mode:
            updates["unifi_poe_mode"] = port.poe_mode

        if updates:
            if hasattr(iface, "snapshot"):
                iface.snapshot()
            data.update(updates)
            iface.custom_field_data = data
            iface.save(update_fields=["custom_field_data"])
            log.info(
                "Syncer: updated PoE config for %s.%s (enabled=%s, mode=%s)",
                iface.device.name, iface.name, port.poe_enabled, port.poe_mode,
            )

    # ------------------------------------------------------------------
    # IPAM sync (site-level VLANs / subnets)
    # ------------------------------------------------------------------

    @staticmethod
    def _network_is_syncable(cn: CollectedNetwork) -> bool:
        purpose = (cn.purpose or "").lower()
        return not any(word in purpose for word in _SKIP_NETWORK_PURPOSES)

    @staticmethod
    def _split_subnet(subnet: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        """
        UniFi reports a network's subnet as either "192.168.10.0/24" or with
        the gateway address, "192.168.10.1/24".  Returns
        (network_cidr, gateway_ip_or_None).
        """
        if not subnet:
            return None, None
        try:
            iface = ipaddress.ip_interface(subnet)
        except ValueError:
            return None, None
        net = iface.network
        gateway = str(iface.ip) if iface.ip != net.network_address else None
        return str(net), gateway

    def _get_vlan_group(self, site):
        """
        Get-or-create a VLANGroup scoped to this site, named
        "{site} - UniFi - VLANs", so that VLAN IDs collected from different
        UniFi sites don't collide, and never mix with Meraki's "{site} VLANs"
        group on a shared site.  Uses generic scope (scope_type/scope_id) when the running
        NetBox version supports it; falls back to an unscoped, name-only group
        on older schemas.
        """
        from ipam.models import VLAN, VLANGroup

        if self.dry_run:
            return None

        name = vlan_group_name(site)
        group = VLANGroup.objects.filter(name=name).first()
        if group is not None:
            return group

        group = self._adopt_legacy(
            VLANGroup, f"{site.name} VLANs", name,
            lambda old: [VLAN.objects.filter(group=old)],
        )
        if group is not None:
            return group

        field_names = {f.name for f in VLANGroup._meta.get_fields()}
        kwargs: dict = {"name": name, "slug": slugify(name)}

        if {"scope_type", "scope_id"} <= field_names:
            from django.contrib.contenttypes.models import ContentType
            kwargs["scope_type"] = ContentType.objects.get_for_model(site)
            kwargs["scope_id"] = site.pk
        elif self._is_settable_fk(VLANGroup, "site"):
            kwargs["site"] = site

        try:
            group, _ = VLANGroup.objects.get_or_create(name=name, defaults=kwargs)
        except Exception as exc:
            log.warning("Syncer: could not create VLANGroup %s: %s", name, exc)
            return None

        self._apply_site_scope(group, site)
        return group

    def _get_or_create_vrf(self, site):
        """Get or create a VRF named '{site_name} - UniFi - VRF' for this site."""
        from ipam.models import VRF, IPAddress, IPRange, Prefix

        if self.dry_run or site is None:
            return None

        name = vrf_name(site)
        vrf = VRF.objects.filter(name=name).first()
        if vrf is None:
            vrf = self._adopt_legacy(
                VRF, f"{site.name} VRF", name,
                lambda old: [
                    Prefix.objects.filter(vrf=old),
                    IPRange.objects.filter(vrf=old),
                    IPAddress.objects.filter(vrf=old),
                ],
                rename_slug=False,
            )
        if vrf is None:
            vrf = VRF.objects.create(name=name)
            log.info("Syncer: created VRF %s", name)
        return vrf

    def _adopt_legacy(self, model, old_name, new_name, children_of, *, rename_slug=True):
        """
        One-time rename of a container an earlier version of this plugin
        created under the old, un-prefixed name ("{site} VRF" etc.).  Only
        adopted when everything inside it carries our unifi tag (and it
        isn't empty), so a container Meraki shares or owns is never touched.
        """
        old = model.objects.filter(name=old_name).first()
        if old is None:
            return None
        querysets = children_of(old)
        if any(qs.exclude(tags__slug=_UNIFI_TAG_SLUG).exists() for qs in querysets):
            return None
        if not any(qs.exists() for qs in querysets):
            return None
        if hasattr(old, "snapshot"):
            old.snapshot()
        old.name = new_name
        fields = ["name"]
        if rename_slug and hasattr(old, "slug"):
            old.slug = slugify(new_name)
            fields.append("slug")
        old.save(update_fields=fields)
        log.info("Syncer: renamed %s %r -> %r", model.__name__, old_name, new_name)
        return old

    def _owned(self, obj) -> bool:
        """True if this plugin created/manages the object (carries the unifi tag)."""
        return obj.tags.filter(slug=_UNIFI_TAG_SLUG).exists()

    def _apply_site_scope(self, obj, site, *, claim: bool = True) -> None:
        """
        Inherit tenant + tags from the parent Site onto a synced object, the
        same way _sync_device does for Devices: the unifi tag plus every
        tag assigned to the Site, and the Site's Tenant.  With claim=False the
        unifi tag is withheld — used for shared objects another plugin
        created, so they aren't mistaken for ours on the next run.
        """
        if self.dry_run or obj is None:
            return

        tenant = site.tenant if site else None
        if self._is_settable_fk(type(obj), "tenant") and obj.tenant_id != (
            tenant.pk if tenant else None
        ):
            if hasattr(obj, "snapshot"):
                obj.snapshot()
            obj.tenant = tenant
            obj.save(update_fields=["tenant"])

        tags_to_add = []
        if claim:
            tag = self._get_unifi_tag()
            if tag:
                tags_to_add.append(tag)
        if site and site.tags.exists():
            tags_to_add.extend(list(site.tags.all()))
        if tags_to_add:
            obj.tags.add(*tags_to_add)

    def _sync_vlan(self, cn: CollectedNetwork, site, vlan_group, vrf=None):
        """
        Get-or-create an ipam.VLAN for this network.  Networks without a VLAN
        ID ("single LAN") don't map to a real 802.1Q VLAN, so no VLAN object
        is created for those — only the Prefix is.
        """
        from ipam.models import VLAN

        vid = cn.vlan_id or 0
        if vid == 0:
            return None
        if self.dry_run:
            self.log.vlans_synced += 1
            return None

        name = cn.name or f"VLAN {vid}"
        lookup: dict = {"vid": vid}
        if vlan_group is not None:
            lookup["group"] = vlan_group
        elif self._is_settable_fk(VLAN, "site"):
            lookup["site"] = site

        vlan = VLAN.objects.filter(**lookup).first()
        if vlan is None:
            create_kwargs = dict(lookup)
            create_kwargs["name"] = name
            create_kwargs["status"] = "active"
            if vlan_group is None and self._is_settable_fk(VLAN, "site"):
                create_kwargs["site"] = site
            if vrf is not None and self._is_settable_fk(VLAN, "vrf"):
                create_kwargs["vrf"] = vrf
            vlan = VLAN.objects.create(**create_kwargs)
            self.log.vlans_synced += 1
            log.info("Syncer: created VLAN %s (%s)", vid, name)
            self._apply_site_scope(vlan, site)
        elif self._owned(vlan):
            if hasattr(vlan, "snapshot"):
                vlan.snapshot()  # pre-change state for changelog diff
            changed = []
            if vlan.name != name:
                vlan.name = name
                changed.append("name")
            if vrf is not None and self._is_settable_fk(VLAN, "vrf") and vlan.vrf_id != vrf.pk:
                vlan.vrf = vrf
                changed.append("vrf")
            if changed:
                vlan.save(update_fields=changed)
                self.log.vlans_synced += 1
            self._apply_site_scope(vlan, site)
        else:
            # A VLAN another plugin (e.g. Meraki) created: reuse it, don't rename it.
            self._apply_site_scope(vlan, site, claim=False)
        return vlan

    def _sync_prefix(self, cn: CollectedNetwork, site, nb_vlan, vrf=None) -> None:
        """Get-or-create an ipam.Prefix for this network's subnet."""
        network, _gateway = self._split_subnet(cn.subnet)
        if not network or self.dry_run:
            return
        self._get_or_create_prefix(network, site, vlan=nb_vlan, vrf=vrf)

    def _sync_static_route(self, route: CollectedRoute, site, vrf=None) -> None:
        """
        Get-or-create an ipam.Prefix (and matching ipam.IPRange) for a UniFi
        static route's destination network, linked to an existing VLAN when a
        Prefix for the same subnet already carries one.  Disabled routes are
        skipped.
        """
        network, _gateway = self._split_subnet(route.network)
        if not network or not route.enabled:
            return

        description = route.name
        if route.nexthop:
            description = f"{route.name} (static route via {route.nexthop})"

        if self.dry_run:
            self.log.static_routes_synced += 1
            return

        vlan = self._find_vlan_for_subnet(network, site, vrf)
        prefix = self._get_or_create_prefix(
            network, site, vlan=vlan, description=description,
            is_static_route=True, vrf=vrf,
        )
        if prefix is not None:
            self.log.static_routes_synced += 1

        self._sync_ip_range_for_subnet(network, site, description=description, vrf=vrf)

    def _find_vlan_by_id(self, vlan_id: int, site) -> Optional[object]:
        """
        Look up a VLAN by its 802.1Q VLAN ID (vid) in this site's UniFi
        VLANGroup only — never another group's VLAN, or an SSID could end up
        attached to a Meraki VLAN.  Used to attach a bridged SSID's WirelessLAN
        to the VLAN its clients actually land on.
        """
        from ipam.models import VLAN

        try:
            vlan_group = self._get_vlan_group(site)
            if vlan_group is None:
                return None
            return VLAN.objects.filter(vid=vlan_id, group=vlan_group).first()
        except Exception as exc:
            log.debug("Syncer: failed to find VLAN %s for site %s: %s", vlan_id, site, exc)
            return None

    def _find_vlan_for_subnet(self, subnet: str, site, vrf=None) -> Optional[object]:
        """
        Find the VLAN linked to an existing Prefix for this subnet (within the
        site VRF).  Used to link static-route Prefixes to the VLAN that
        already carries that subnet.
        """
        from ipam.models import Prefix

        try:
            prefix_obj = Prefix.objects.filter(prefix=subnet, vrf=vrf).first()
            if prefix_obj and prefix_obj.vlan_id:
                return prefix_obj.vlan
        except Exception as exc:
            log.debug("Syncer: failed to find VLAN for subnet %s: %s", subnet, exc)
        return None

    def _get_or_create_prefix(
        self, subnet: str, site, *, vlan=None, description: str = "",
        is_static_route: bool = False, vrf=None,
    ):
        """
        Shared get-or-create for ipam.Prefix, used by both network subnets and
        static-route subnets.  Prefixes are scoped to the per-site VRF.  An
        existing prefix this plugin owns is updated; one another plugin
        created is only gap-filled (VLAN / description if blank).
        """
        from ipam.models import Prefix

        if self.dry_run:
            if not is_static_route:
                self.log.prefixes_synced += 1
            return None

        prefix = Prefix.objects.filter(prefix=subnet, vrf=vrf).first()
        if prefix is None:
            create_kwargs: dict = {"prefix": subnet, "status": "active", "vrf": vrf}
            if vlan is not None:
                create_kwargs["vlan"] = vlan
            if description:
                create_kwargs["description"] = description
            if {"scope_type", "scope_id"} <= {f.name for f in Prefix._meta.get_fields()}:
                from django.contrib.contenttypes.models import ContentType
                create_kwargs["scope_type"] = ContentType.objects.get_for_model(site)
                create_kwargs["scope_id"] = site.pk
            elif self._is_settable_fk(Prefix, "site"):
                create_kwargs["site"] = site
            # Let creation errors propagate to sync_ipam's per-item try/except
            # so the real cause gets surfaced to the console.
            prefix = Prefix.objects.create(**create_kwargs)
            if not is_static_route:
                self.log.prefixes_synced += 1
            log.info("Syncer: created prefix %s", subnet)
            self._apply_site_scope(prefix, site)
            return prefix

        owned = self._owned(prefix)
        if hasattr(prefix, "snapshot"):
            prefix.snapshot()  # pre-change state for changelog diff
        changed = []
        if vlan is not None and prefix.vlan_id != vlan.pk and (owned or prefix.vlan_id is None):
            prefix.vlan = vlan
            changed.append("vlan")
        if description and prefix.description != description and (owned or not prefix.description):
            prefix.description = description
            changed.append("description")
        if changed:
            prefix.save(update_fields=changed)
            if not is_static_route:
                self.log.prefixes_synced += 1

        self._apply_site_scope(prefix, site, claim=owned)
        return prefix

    def _sync_vlan_ip(self, cn: CollectedNetwork, site, vrf=None) -> None:
        """
        Create the IPAddress for the gateway's interface on this network (when
        UniFi reported it as the host part of the subnet), and attach it to a
        matching gateway interface if one exists ("LAN" for a network without
        a VLAN, "VLAN{N}" otherwise).
        """
        network, gateway = self._split_subnet(cn.subnet)
        if not gateway or not network or self.dry_run:
            return

        from ipam.models import IPAddress as IPAddr
        from dcim.models import Interface
        from django.contrib.contenttypes.models import ContentType

        prefix_length = network.split("/", 1)[1]
        address = f"{gateway}/{prefix_length}"

        vid = cn.vlan_id or 0
        iface_name = "LAN" if vid == 0 else f"VLAN{vid}"
        iface = Interface.objects.filter(
            device__site=site,
            device__tags__slug=_UNIFI_TAG_SLUG,
            device__role__slug="firewall",
            name=iface_name,
        ).first()

        ip_obj = IPAddr.objects.filter(address=address, vrf=vrf).first()
        if ip_obj is None:
            create_kwargs: dict = {"address": address, "status": "active", "vrf": vrf}
            if iface is not None:
                create_kwargs["assigned_object_type"] = ContentType.objects.get_for_model(iface)
                create_kwargs["assigned_object_id"] = iface.pk
            ip_obj = IPAddr.objects.create(**create_kwargs)
            self.log.ips_synced += 1
        elif ip_obj.assigned_object_id is None and iface is not None:
            if hasattr(ip_obj, "snapshot"):
                ip_obj.snapshot()
            ip_obj.assigned_object_type = ContentType.objects.get_for_model(iface)
            ip_obj.assigned_object_id = iface.pk
            ip_obj.save(update_fields=["assigned_object_type", "assigned_object_id"])

        self._apply_site_scope(ip_obj, site)

    def _sync_vlan_ip_range(self, cn: CollectedNetwork, site, vrf=None) -> None:
        """
        Create an ipam.IPRange spanning this network's usable host addresses.
        UniFi's DHCP range, when reported, is folded into the range's
        description for visibility.
        """
        network, _gateway = self._split_subnet(cn.subnet)
        if not network:
            return
        description = ""
        if cn.dhcp_start and cn.dhcp_stop:
            description = f"DHCP {cn.dhcp_start} - {cn.dhcp_stop}"
        self._sync_ip_range_for_subnet(network, site, description=description, vrf=vrf)

    def _sync_ip_range_for_subnet(
        self, subnet: str, site, *, description: str = "", vrf=None,
    ) -> None:
        """
        Shared get-or-create for an ipam.IPRange spanning a subnet's usable
        host addresses (excluding the network and broadcast addresses), so the
        pool is represented distinctly from individual device IPAddress
        records.  Used for both network subnets and static-route subnets.
        """
        import netaddr
        from ipam.models import IPRange

        if not subnet or self.dry_run:
            return

        try:
            network = ipaddress.ip_network(subnet, strict=False)
        except ValueError:
            log.debug("Syncer: could not parse subnet %s for IP range", subnet)
            return

        hosts = list(network.hosts()) if network.num_addresses <= 65536 else []
        if not hosts:
            return  # e.g. /31, /32 or huge IPv6 — no usable range to represent

        start_address = netaddr.IPNetwork(f"{hosts[0]}/{network.prefixlen}")
        end_address   = netaddr.IPNetwork(f"{hosts[-1]}/{network.prefixlen}")

        ip_range = IPRange.objects.filter(
            start_address=start_address, end_address=end_address, vrf=vrf,
        ).first()

        if ip_range is None:
            try:
                create_kwargs = {
                    "start_address": start_address,
                    "end_address": end_address,
                    "status": "active",
                    "vrf": vrf,
                }
                if description:
                    create_kwargs["description"] = description
                with transaction.atomic():
                    ip_range = IPRange.objects.create(**create_kwargs)
                log.info("Syncer: created IP range %s-%s", start_address, end_address)
                self._apply_site_scope(ip_range, site)
            except Exception as exc:
                log.warning("Syncer: failed to create IP range for %s: %s", subnet, exc)
            return

        owned = self._owned(ip_range)
        if description and ip_range.description != description and (owned or not ip_range.description):
            if hasattr(ip_range, "snapshot"):
                ip_range.snapshot()
            ip_range.description = description
            ip_range.save(update_fields=["description"])
        self._apply_site_scope(ip_range, site, claim=owned)

    @staticmethod
    def _is_settable_fk(model, name: str) -> bool:
        """
        True only if `name` is a real forward FK/O2O field that can be passed
        as a constructor kwarg.  Django's `_meta.get_fields()` also returns
        reverse relations and M2M accessors, which can't be assigned directly.
        """
        try:
            field = model._meta.get_field(name)
        except Exception:
            return False
        return bool(getattr(field, "many_to_one", False) or getattr(field, "one_to_one", False))

    # ------------------------------------------------------------------
    # Wireless
    # ------------------------------------------------------------------

    def _get_wireless_lan_group(self, site):
        """Get-or-create a WirelessLANGroup scoped to this site, mirroring _get_vlan_group."""
        from wireless.models import WirelessLANGroup

        if self.dry_run:
            return None

        name = wlan_group_name(site)
        group = WirelessLANGroup.objects.filter(name=name).first()
        if group is not None:
            return group

        from wireless.models import WirelessLAN
        group = self._adopt_legacy(
            WirelessLANGroup, f"{site.name} WLANs", name,
            lambda old: [WirelessLAN.objects.filter(group=old)],
        )
        if group is not None:
            return group

        field_names = {f.name for f in WirelessLANGroup._meta.get_fields()}
        kwargs: dict = {"name": name, "slug": slugify(name)}

        if {"scope_type", "scope_id"} <= field_names:
            from django.contrib.contenttypes.models import ContentType
            kwargs["scope_type"] = ContentType.objects.get_for_model(site)
            kwargs["scope_id"] = site.pk
        elif self._is_settable_fk(WirelessLANGroup, "site"):
            kwargs["site"] = site

        try:
            group, _ = WirelessLANGroup.objects.get_or_create(name=name, defaults=kwargs)
        except Exception as exc:
            log.warning("Syncer: could not create WirelessLANGroup %s: %s", name, exc)
            return None

        self._apply_site_scope(group, site)
        return group

    @staticmethod
    def _wlan_description(cw: CollectedWlan) -> str:
        parts = ["UniFi SSID"]
        if cw.hidden:
            parts.append("(hidden)")
        if cw.band_steering:
            parts.append("band-steering enabled")
        if cw.client_limit and cw.client_limit > 0:
            parts.append(f"max {cw.client_limit} clients")
        return " ".join(parts)

    @staticmethod
    def _map_unifi_security(security: Optional[str]) -> str:
        """Best-effort map of UniFi's wlanconf `security` -> NetBox WirelessAuthTypeChoices."""
        if not security:
            return ""
        if security == "open":
            return "open"
        if security == "wpapsk":
            return "wpa-personal"
        if security == "wpaeap":
            return "wpa-enterprise"
        return ""

    def _sync_wireless_lan(self, cw: CollectedWlan, site, wlan_group):
        """
        Get-or-create a wireless.WirelessLAN for one collected SSID.  If the
        SSID is bridged to a single VLAN, that VLAN is looked up in this site's
        VLANGroup and attached, so NetBox reflects which subnet the SSID's
        clients actually land on.
        """
        from wireless.models import WirelessLAN

        if self.dry_run:
            self.log.wireless_lans_synced += 1
            return None

        lookup: dict = {"ssid": cw.ssid}
        if wlan_group is not None:
            lookup["group"] = wlan_group

        wlan = WirelessLAN.objects.filter(**lookup).first()
        auth_type = self._map_unifi_security(cw.security)
        vlan = self._find_vlan_by_id(cw.vlan_id, site) if cw.vlan_id else None
        description = self._wlan_description(cw)

        if wlan is None:
            create_kwargs = dict(lookup)
            create_kwargs["status"] = "active"
            create_kwargs["description"] = description
            if auth_type:
                create_kwargs["auth_type"] = auth_type
            if vlan is not None:
                create_kwargs["vlan"] = vlan
            wlan = WirelessLAN.objects.create(**create_kwargs)
            self.log.wireless_lans_synced += 1
            log.info("Syncer: created WirelessLAN %s", cw.ssid)
        else:
            if hasattr(wlan, "snapshot"):
                wlan.snapshot()  # pre-change state for changelog diff
            changed = []
            if auth_type and wlan.auth_type != auth_type:
                wlan.auth_type = auth_type
                changed.append("auth_type")
            if wlan.status != "active":
                wlan.status = "active"
                changed.append("status")
            if wlan.description != description:
                wlan.description = description
                changed.append("description")
            if vlan is not None and wlan.vlan_id != vlan.pk:
                wlan.vlan = vlan
                changed.append("vlan")
            if changed:
                wlan.save(update_fields=changed)
            self.log.wireless_lans_synced += 1

        self._apply_site_scope(wlan, site)
        return wlan

    def _attach_wireless_lans_to_aps(self, wlans: list, site) -> None:
        """
        Attach every synced WirelessLAN to the radio interface of each UniFi
        access point already synced at this site (UniFi SSIDs broadcast
        site-wide, so there's no per-AP SSID assignment to read here).  Only
        radios on devices carrying the unifi tag are touched.
        """
        from dcim.models import Interface

        radios = Interface.objects.filter(
            device__site=site,
            device__tags__slug=_UNIFI_TAG_SLUG,
            name=_RADIO_IFACE_NAME,
        )
        for iface in radios:
            iface.wireless_lans.add(*wlans)

    # ------------------------------------------------------------------
    # Lazy-loaded shared objects
    # ------------------------------------------------------------------

    def _get_manufacturer(self):
        if self._manufacturer is None:
            from dcim.models import Manufacturer
            self._manufacturer, _ = Manufacturer.objects.get_or_create(
                name=_UBIQUITI_MANUFACTURER,
                defaults={"slug": slugify(_UBIQUITI_MANUFACTURER)},
            )
        return self._manufacturer

    def _get_device_type(self, model: str, manufacturer):
        model = model or "Unknown UniFi Device"
        if model not in self._device_type_cache:
            from dcim.models import DeviceType
            dt = (
                DeviceType.objects.filter(manufacturer=manufacturer, model__iexact=model).first()
                or DeviceType.objects.filter(manufacturer=manufacturer, slug=slugify(model)).first()
            )
            created = False
            if dt is None:
                dt = DeviceType.objects.create(
                    manufacturer=manufacturer, model=model, slug=slugify(model),
                )
                created = True
            if created and self._devicetype_library is not None:
                self._enrich_device_type(dt, model)
            self._device_type_cache[model] = dt
        return self._device_type_cache[model]

    def backfill_device_types(self) -> tuple[int, int]:
        """
        Enrich every existing Ubiquiti DeviceType that hasn't been enriched
        yet — for DeviceTypes created before enable_devicetype_library was
        turned on, or during a run where the lookup failed (e.g. GitHub
        unreachable).

        A DeviceType only counts as "already enriched" if it has a u_height
        other than NetBox's default of 1 OR a front/rear image OR non-empty
        comments — so this is safe to run repeatedly and never overwrites
        anything you've edited by hand.

        Returns (enriched_count, skipped_count).
        """
        from dcim.models import DeviceType

        if self._devicetype_library is None:
            log.warning("Syncer: backfill_device_types called without enable_devicetype_library set.")
            return (0, 0)

        manufacturer = self._get_manufacturer()
        enriched, skipped = 0, 0
        for dt in DeviceType.objects.filter(manufacturer=manufacturer):
            already_enriched = (
                dt.u_height != 1
                or bool(dt.front_image)
                or bool(dt.rear_image)
                or bool(dt.comments)
            )
            if already_enriched:
                skipped += 1
                continue
            self._enrich_device_type(dt, dt.model)
            dt.refresh_from_db()
            if dt.u_height != 1 or dt.front_image or dt.rear_image or dt.comments:
                enriched += 1
            else:
                skipped += 1
        return (enriched, skipped)

    def _enrich_device_type(self, device_type, model: str) -> None:
        """
        Fill in u_height, weight, airflow, part_number, comments, and
        front/rear elevation images from the NetBox Community Device Type
        Library, for a DeviceType this sync just created.  Only runs once per
        model (on creation), so hand edits made in NetBox afterward are never
        overwritten by a later sync.
        """
        try:
            definition = self._devicetype_library.find_definition(model)
        except Exception as exc:
            log.warning("Device Type Library: lookup failed for %s: %s", model, exc)
            return
        if not definition:
            log.debug("Device Type Library: no match found for model %s", model)
            return

        fields = []
        for field, key in (
            ("u_height", "u_height"),
            ("is_full_depth", "is_full_depth"),
            ("airflow", "airflow"),
            ("weight", "weight"),
            ("weight_unit", "weight_unit"),
            ("part_number", "part_number"),
            ("comments", "comments"),
        ):
            value = definition.get(key)
            if value is not None:
                setattr(device_type, field, value)
                fields.append(field)

        # Images are best-effort: a download or file-permission problem (e.g.
        # NetBox's media directory not writable by the user running the sync)
        # must never stop the device itself from being created.
        for face, attr in (("front", "front_image"), ("rear", "rear_image")):
            try:
                image = self._devicetype_library.fetch_image(definition, face)
                if image:
                    filename, content = image
                    from django.core.files.base import ContentFile
                    getattr(device_type, attr).save(filename, ContentFile(content), save=False)
                    fields.append(attr)
            except OSError as exc:
                log.warning(
                    "Device Type Library: could not save %s image for %s: %s "
                    "(check that NetBox's media directory is writable by the user running the sync)",
                    face, model, exc,
                )
                setattr(device_type, attr, None)

        if fields:
            device_type.save()
            log.info(
                "Syncer: enriched DeviceType %s from devicetype-library (%s)",
                model, ", ".join(fields),
            )

    def _get_role(self, family: str):
        """
        Return a DeviceRole appropriate for the UniFi device family.  Roles are
        created automatically if absent.  Slugs match the Meraki plugin's
        (switch / firewall / ap) so both vendors' gear share roles.
        """
        role_map = {
            "switch":  ("switch",   "Switch",       "2196f3"),
            "gateway": ("firewall", "Firewall",     "f44336"),
            "ap":      ("ap",       "Access Point", "4caf50"),
        }
        slug, name, color = role_map.get(
            family, (self.default_role_slug, "Network", "0080ff")
        )
        if slug not in self._role_cache:
            from dcim.models import DeviceRole
            role, _ = DeviceRole.objects.get_or_create(
                slug=slug,
                defaults={"name": name, "color": color},
            )
            self._role_cache[slug] = role
        return self._role_cache[slug]

    def _get_unifi_tag(self):
        """
        The "unifi" tag.  An earlier version called it "src:unifi"; if that
        tag exists and "unifi" doesn't, it is renamed in place, so every object
        already carrying it keeps it (no duplicate tag, nothing re-tagged).
        """
        if self._unifi_tag is None and not self.dry_run:
            from extras.models import Tag
            tag = Tag.objects.filter(slug=_UNIFI_TAG_SLUG).first()
            if tag is None:
                legacy = Tag.objects.filter(slug=_LEGACY_TAG_SLUG).first()
                if legacy is not None and not Tag.objects.filter(name=_UNIFI_TAG_NAME).exists():
                    if hasattr(legacy, "snapshot"):
                        legacy.snapshot()
                    legacy.name = _UNIFI_TAG_NAME
                    legacy.slug = _UNIFI_TAG_SLUG
                    legacy.save()
                    log.info("Syncer: renamed tag 'src:unifi' -> 'unifi'")
                    tag = legacy
            if tag is None:
                tag = Tag.objects.create(
                    slug=_UNIFI_TAG_SLUG, name=_UNIFI_TAG_NAME, color=_UNIFI_TAG_COLOR,
                )
            self._unifi_tag = tag
        return self._unifi_tag

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _iface_type(family: str, speed_mbps: Optional[int]) -> str:
        """Derive the best NetBox interface type from family and link speed."""
        if speed_mbps and speed_mbps in _SPEED_TO_IFACE_TYPE:
            return _SPEED_TO_IFACE_TYPE[speed_mbps]
        return _FAMILY_IFACE_TYPE.get(family, "other")

    def _get_vlan_by_id(self, vlan_id: Optional[int], site):
        """Get a VLAN by ID within the given site's UniFi VLANGroup."""
        if not vlan_id:
            return None
        from ipam.models import VLAN, VLANGroup

        if not site:
            return None
        vlan_group = VLANGroup.objects.filter(name=vlan_group_name(site)).first()
        if vlan_group is None:
            return None   # never fall back to a VLAN from someone else's group
        return VLAN.objects.filter(vid=vlan_id, group=vlan_group).first()

    def _get_vlans_by_ids(self, vlan_ids: list[int], site) -> list:
        vlans = []
        for vid in vlan_ids or []:
            vlan = self._get_vlan_by_id(vid, site)
            if vlan:
                vlans.append(vlan)
        return vlans

    @staticmethod
    def _ensure_device_cf(name: str, label: str) -> None:
        """Create a text custom field on dcim.device if it does not exist."""
        ensure_custom_field("dcim", "device", name, label)

    @staticmethod
    def _ensure_interface_poe_cf() -> None:
        """Create the PoE custom fields on dcim.interface if they don't exist."""
        ensure_custom_field("dcim", "interface", "unifi_poe_enabled", "UniFi PoE Enabled", cf_type="boolean")
        ensure_custom_field("dcim", "interface", "unifi_poe_mode", "UniFi PoE Mode")


# ---------------------------------------------------------------------------
# Site helpers (used by the management command)
# ---------------------------------------------------------------------------

def get_mapped_sites() -> list:
    """
    Return all NetBox Site objects that have unifi_site_id set.
    Each returned object has .custom_field_data["unifi_site_id"] populated.
    """
    from dcim.models import Site

    return [
        site for site in Site.objects.all()
        if (site.custom_field_data or {}).get("unifi_site_id")
    ]


def update_site_unifi_name(site, site_name: str) -> None:
    """Back-fill unifi_site_name on the site when it differs."""
    if not site_name:
        return
    data = dict(site.custom_field_data or {})
    if data.get("unifi_site_name") != site_name:
        data["unifi_site_name"] = site_name
        site.custom_field_data = data
        site.save(update_fields=["custom_field_data"])
