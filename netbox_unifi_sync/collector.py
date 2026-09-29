"""UniFi collector: fetches from both APIs and returns CollectedSite objects.

Integration API  — primary source for sites, devices, networks, WLANs.
Classic API      — supplements with serial, firmware, port_table, portconf,
                   LLDP, uplink/WAN, static routes.

If classic credentials are not configured the collector still works, but
Interface mode/VLAN, PoE, serial, and route data won't be available.
"""
from __future__ import annotations

import ipaddress
import logging
import re
from typing import Optional

from .dataclasses import (
    CollectedDevice,
    CollectedInterface,
    CollectedNetwork,
    CollectedRoute,
    CollectedSite,
    CollectedWlan,
)
from .unifi_api import ClassicClient, IntegrationClient, UnifiNotFound

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model → DeviceType slug helpers
# ---------------------------------------------------------------------------

# Map known Integration API model strings to the slugs you use in NetBox.
# Add rows as you encounter new models. Keys are case-insensitive substrings.
_MODEL_SLUG_MAP = [
    ("US 48 PoE 500W",   "us-48-poe-500w"),
    ("US 48",            "us-48"),
    ("US 24 PoE",        "us-24-poe"),
    ("US 24",            "us-24"),
    ("US 16",            "us-16"),
    ("US 8",             "us-8"),
    ("USW-Pro-48-PoE",   "usw-pro-48-poe"),
    ("USW-Pro-24-PoE",   "usw-pro-24-poe"),
    ("USW-Pro-24",       "usw-pro-24"),
    ("USW-Lite",         "usw-lite"),
    ("USW-Flex",         "usw-flex"),
    ("USW-Enterprise",   "usw-enterprise"),
    ("USG-Pro-4",        "usg-pro-4"),
    ("USG-3P",           "usg-3p"),
    ("UDM-Pro",          "udm-pro"),
    ("UDM-SE",           "udm-se"),
    ("UDM",              "udm"),
    ("U6-Pro",           "u6-pro"),
    ("U6-LR",            "u6-lr"),
    ("U6-Lite",          "u6-lite"),
    ("U6-Mesh",          "u6-mesh"),
    ("UAP-AC-Pro",       "uap-ac-pro"),
    ("UAP-AC-LR",        "uap-ac-lr"),
    ("UAP-AC-Lite",      "uap-ac-lite"),
    ("UAP-AC-M",         "uap-ac-m"),
]


def _model_to_slug(model: str) -> str:
    """Best-effort model string → NetBox DeviceType slug."""
    for pattern, slug in _MODEL_SLUG_MAP:
        if pattern.lower() in model.lower():
            return slug
    # Fallback: lowercase, replace spaces/slashes with hyphens
    slug = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")
    return slug


def _to_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _classic_vlan(nc: dict) -> Optional[int]:
    """VLAN ID of a classic networkconf/wlanconf entry (`vlan`, or legacy `vlan_id`/`vlanid`)."""
    if nc.get("vlan_enabled") is False:
        return None
    for key in ("vlan", "vlan_id", "vlanid"):
        vid = _to_int(nc.get(key))
        if vid:
            return vid
    return None


def _device_family(model: str, classic_type: Optional[str] = None) -> str:
    """Classify a device as "switch", "gateway", "ap" or "other"."""
    t = (classic_type or "").lower()
    if t == "usw":
        return "switch"
    if t in ("ugw", "udm", "uxg", "ucg"):
        return "gateway"
    if t in ("uap", "ubb"):
        return "ap"
    m = (model or "").upper().replace(" ", "")
    if m.startswith(("USW", "US-", "US8", "US1", "US2", "US4", "USL")):
        return "switch"
    if m.startswith(("USG", "UDM", "UXG", "UCG", "UDR", "UDW")):
        return "gateway"
    if m.startswith(("UAP", "U6", "U7", "UWB", "E7", "UAL", "U-")):
        return "ap"
    return "other"


def _mac_to_display(mac: str) -> str:
    """Normalise MAC to xx:xx:xx:xx:xx:xx format."""
    clean = re.sub(r"[^0-9a-fA-F]", "", mac)
    if len(clean) != 12:
        return mac
    return ":".join(clean[i:i+2] for i in range(0, 12, 2)).lower()


def _speed_mbps_to_kbps(speed_mbps: Optional[int]) -> Optional[int]:
    """Convert Mbps (UniFi classic) to kbps (NetBox Interface.speed)."""
    if speed_mbps is None:
        return None
    return speed_mbps * 1000


def _parse_prefix_length(subnet: str) -> int:
    """Extract prefix length from CIDR, e.g. '192.168.1.1/24' → 24."""
    try:
        return int(subnet.split("/")[1])
    except (IndexError, ValueError):
        return 24


def _mgmt_ip_from_subnet(ip_with_subnet: Optional[str]) -> tuple[Optional[str], int]:
    """Split '192.168.1.1/24' into ('192.168.1.1', 24)."""
    if not ip_with_subnet:
        return None, 24
    if "/" in ip_with_subnet:
        ip, prefix = ip_with_subnet.split("/", 1)
        return ip.strip(), int(prefix)
    return ip_with_subnet.strip(), 24


# ---------------------------------------------------------------------------
# Port VLAN mode resolution
# ---------------------------------------------------------------------------

def _resolve_port_mode(portconf: Optional[dict], netconf_by_id: dict) -> tuple[
    Optional[str], Optional[int], list[int]
]:
    """
    Translate a UniFi portconf entry into NetBox Interface (mode, untagged_vid, tagged_vids).

    UniFi portconf forward values:
      "all"       → tagged-all (trunk carrying every VLAN)
      "blocked"   → access on native VLAN, no trunking
      "customize" → specific tagged + excluded lists

    Returns (mode, untagged_vlan_vid, [tagged_vlan_vids])
    """
    if not portconf:
        return None, None, []

    forward = portconf.get("forward", "all")
    native_id = portconf.get("native_networkconf_id")
    tagged_ids = portconf.get("tagged_networkconf_ids") or []
    excluded_ids = set(portconf.get("excluded_networkconf_ids") or [])

    native_vid = None
    if native_id and native_id in netconf_by_id:
        native_vid = _classic_vlan(netconf_by_id[native_id])

    if forward == "all":
        # Trunk: all VLANs except excluded
        if excluded_ids:
            tagged_vids = [
                _classic_vlan(nc)
                for nc_id, nc in netconf_by_id.items()
                if nc_id not in excluded_ids and _classic_vlan(nc)
            ]
            return "tagged", native_vid, sorted(set(tagged_vids))
        return "tagged-all", native_vid, []

    if forward in ("native", "blocked", "disabled"):
        # Untagged on the native VLAN only
        return "access", native_vid, []

    if forward == "customize":
        if tagged_ids:
            tagged_vids = [
                _classic_vlan(netconf_by_id[nc_id])
                for nc_id in tagged_ids
                if nc_id in netconf_by_id and _classic_vlan(netconf_by_id[nc_id])
            ]
        else:
            # Newer firmware lists what is *excluded* instead
            tagged_vids = [
                _classic_vlan(nc)
                for nc_id, nc in netconf_by_id.items()
                if nc_id not in excluded_ids and nc_id != native_id and _classic_vlan(nc)
            ]
        tagged_vids = sorted(set(v for v in tagged_vids if v != native_vid))
        return ("tagged" if tagged_vids else "access"), native_vid, tagged_vids

    return None, native_vid, []


# ---------------------------------------------------------------------------
# Main collector
# ---------------------------------------------------------------------------

class UnifiCollector:
    """
    Collects data from one or all UniFi sites and returns CollectedSite objects.

    Usage:
        with IntegrationClient(...) as api, ClassicClient(...) as classic:
            collector = UnifiCollector(api, classic)
            sites = collector.collect()          # all sites
            site  = collector.collect_site(uuid) # one site
    """

    def __init__(
        self,
        integration: IntegrationClient,
        classic: Optional[ClassicClient] = None,
    ):
        self._api = integration
        self._classic = classic

    # --- Public entry points ------------------------------------------------

    def collect(self, site_uuids: Optional[list[str]] = None) -> list[CollectedSite]:
        """Collect all sites (or just the given UUIDs)."""
        all_sites = self._api.sites()
        if site_uuids:
            site_uuids_set = set(site_uuids)
            all_sites = [s for s in all_sites if s["id"] in site_uuids_set]
        return [self._collect_site(s) for s in all_sites]

    def list_sites(self) -> list[dict]:
        """All sites on the controller (Integration API metadata dicts)."""
        return list(self._api.sites())

    @staticmethod
    def resolve_site(ref: str, sites: list[dict]) -> Optional[dict]:
        """
        Find a site by UUID, internal reference (e.g. "clqd72kz") or exact
        name (case-insensitive) — in that order.  Returns None if not found.
        """
        ref = (ref or "").strip()
        if not ref:
            return None
        for s in sites:
            if s.get("id") == ref:
                return s
        for s in sites:
            if s.get("internalReference") == ref:
                return s
        for s in sites:
            if str(s.get("name", "")).strip().lower() == ref.lower():
                return s
        return None

    def collect_site_meta(self, site_meta: dict) -> CollectedSite:
        """Collect one site from an already-fetched site metadata dict."""
        return self._collect_site(site_meta)

    def collect_site(self, site_uuid: str) -> CollectedSite:
        sites = self._api.sites()
        for s in sites:
            if s["id"] == site_uuid:
                return self._collect_site(s)
        raise ValueError(f"Site {site_uuid!r} not found on this controller")

    # --- Site collection ----------------------------------------------------

    def _collect_site(self, site_meta: dict) -> CollectedSite:
        uuid = site_meta["id"]
        ref = site_meta.get("internalReference", "")
        name = site_meta.get("name", ref)
        log.info("Collecting site: %s (%s)", name, uuid)

        site = CollectedSite(
            unifi_site_id=uuid,
            internal_reference=ref,
            name=name,
        )

        # --- Integration API data ---
        int_devices = self._fetch_integration_devices(uuid)
        try:
            int_networks = self._api.networks(uuid)
        except (UnifiNotFound, Exception) as exc:
            log.warning("Integration networks unavailable for %s: %s", name, exc)
            int_networks = []
        try:
            int_wlans = self._api.wifi_broadcasts(uuid)
        except (UnifiNotFound, Exception) as exc:
            log.warning("Integration WLANs unavailable for %s: %s", name, exc)
            int_wlans = []

        # --- Classic API data (optional) ---
        classic_devices_by_mac: dict[str, dict] = {}
        netconf_by_id: dict[str, dict] = {}
        portconf_by_id: dict[str, dict] = {}
        classic_routes: list[dict] = []
        classic_wlans: list[dict] = []

        if self._classic and ref:
            classic_devices_by_mac, netconf_by_id, portconf_by_id, \
                classic_routes, classic_wlans = self._fetch_classic(ref)
        elif not self._classic:
            log.debug("Classic API not configured — serial/port detail unavailable")
        elif not ref:
            log.warning("Site %s has no internalReference — skipping classic API", name)

        # --- Build devices ---
        site.devices = [
            self._build_device(d, classic_devices_by_mac, portconf_by_id, netconf_by_id)
            for d in int_devices
        ]

        # --- Build networks / IPAM ---
        site.networks = self._build_networks(int_networks, netconf_by_id)

        # --- Build static routes ---
        site.routes = [
            CollectedRoute(
                name=r.get("name", r.get("network", "?")),
                network=r.get("network", ""),
                nexthop=r.get("nexthop", ""),
                enabled=r.get("enabled", True),
            )
            for r in classic_routes
            if r.get("network") and r.get("nexthop")
        ]

        # --- Build WLANs (classic is more reliable; Integration fills gaps) ---
        site.wlans = self._build_wlans(classic_wlans, int_wlans, netconf_by_id)

        return site

    # --- Integration API fetches --------------------------------------------

    def _fetch_integration_devices(self, site_id: str) -> list[dict]:
        try:
            return list(self._api.integration_devices(site_id))
        except Exception as exc:
            log.error("Failed to fetch integration devices for %s: %s", site_id, exc)
            return []

    # --- Classic API fetches ------------------------------------------------

    def _fetch_classic(self, ref: str):
        classic_devices_by_mac: dict[str, dict] = {}
        netconf_by_id: dict[str, dict] = {}
        portconf_by_id: dict[str, dict] = {}
        classic_routes: list[dict] = []
        classic_wlans: list[dict] = []

        try:
            for d in (self._classic.devices(ref) or []):
                mac = _mac_to_display(d.get("mac", ""))
                if mac:
                    classic_devices_by_mac[mac] = d
        except Exception as exc:
            log.warning("classic stat/device failed for %s: %s", ref, exc)

        try:
            for nc in (self._classic.networkconf(ref) or []):
                netconf_by_id[nc["_id"]] = nc
        except Exception as exc:
            log.warning("classic networkconf failed for %s: %s", ref, exc)

        try:
            for pc in (self._classic.portconf(ref) or []):
                portconf_by_id[pc["_id"]] = pc
        except Exception as exc:
            log.warning("classic portconf failed for %s: %s", ref, exc)

        try:
            classic_routes = self._classic.routing(ref) or []
        except Exception as exc:
            log.warning("classic routing failed for %s: %s", ref, exc)

        try:
            classic_wlans = self._classic.wlanconf(ref) or []
        except Exception as exc:
            log.warning("classic wlanconf failed for %s: %s", ref, exc)

        return classic_devices_by_mac, netconf_by_id, portconf_by_id, \
               classic_routes, classic_wlans

    # --- Device builder -----------------------------------------------------

    def _build_device(
        self,
        int_dev: dict,
        classic_by_mac: dict[str, dict],
        portconf_by_id: dict[str, dict],
        netconf_by_id: dict[str, dict],
    ) -> CollectedDevice:
        mac = _mac_to_display(int_dev.get("macAddress", ""))
        classic = classic_by_mac.get(mac, {})

        # Management IP: Integration gives a bare IP; classic gives IP with prefix
        mgmt_ip_raw = int_dev.get("ipAddress") or classic.get("ip")
        mgmt_ip, prefix_len = _mgmt_ip_from_subnet(mgmt_ip_raw)
        if mgmt_ip and "/" not in mgmt_ip_raw:
            # Integration gave a bare IP; try to get prefix from classic networkconf
            # Look for a matching network to infer the prefix length
            for nc in netconf_by_id.values():
                try:
                    net = ipaddress.ip_interface(nc.get("ip_subnet") or "").network
                    if ipaddress.ip_address(mgmt_ip) in net:
                        prefix_len = net.prefixlen
                        break
                except ValueError:
                    continue

        # Device status: classic state 1 = connected/active
        state = classic.get("state", int_dev.get("status", {}).get("status", ""))
        status = "active" if str(state) in ("1", "connected") else "offline"

        # Uplink config for gateways (USG, UDM etc.)
        uplink_config = ""
        if classic.get("uplink"):
            uplink = classic["uplink"]
            parts = []
            if uplink.get("name"):
                parts.append(f"{uplink['name']}:primary")
            if classic.get("wan2_ip"):
                parts.append("WAN2:failover")
            uplink_config = ",".join(parts)

        model = int_dev.get("model") or classic.get("model") or ""

        device = CollectedDevice(
            name=int_dev.get("name", mac),
            mac=mac,
            serial=classic.get("serial") or None,
            unifi_device_id=int_dev.get("id", ""),
            model=model,
            device_type_slug=_model_to_slug(model),
            family=_device_family(model, classic.get("type")),
            firmware=classic.get("version") or int_dev.get("firmwareVersion") or "",
            status=status,
            management_ip=mgmt_ip,
            management_prefix_length=prefix_len,
            uplink_config=uplink_config,
        )

        # Build interfaces from classic port_table
        device.interfaces = self._build_interfaces(classic, portconf_by_id, netconf_by_id)
        return device

    # --- Interface builder --------------------------------------------------

    def _build_interfaces(
        self,
        classic: dict,
        portconf_by_id: dict[str, dict],
        netconf_by_id: dict[str, dict],
    ) -> list[CollectedInterface]:
        if not classic:
            return []

        port_table = classic.get("port_table") or []
        # port_overrides: list of {port_idx, portconf_id, poe_mode, ...}
        overrides_by_idx: dict[int, dict] = {
            o["port_idx"]: o
            for o in (classic.get("port_overrides") or [])
        }

        interfaces = []
        for port in port_table:
            idx = port.get("port_idx")
            name = port.get("name") or f"Port {idx}"

            # Skip aggregate / uplink pseudo-ports
            if port.get("is_uplink") and not port.get("port_idx"):
                continue

            override = overrides_by_idx.get(idx, {})
            portconf_id = override.get("portconf_id")
            portconf = portconf_by_id.get(portconf_id) if portconf_id else None

            mode, untagged_vid, tagged_vids = _resolve_port_mode(portconf, netconf_by_id)

            # Speed: classic reports in Mbps; NetBox stores kbps
            speed_raw = port.get("speed")
            speed_kbps = _speed_mbps_to_kbps(speed_raw) if speed_raw else None

            duplex = None
            if port.get("full_duplex") is True:
                duplex = "full"
            elif port.get("full_duplex") is False:
                duplex = "half"

            # PoE
            poe_enabled = port.get("poe_enable")
            poe_mode = override.get("poe_mode") or (
                "auto" if poe_enabled else None
            )

            interfaces.append(CollectedInterface(
                name=name,
                port_idx=idx,
                speed=speed_kbps,
                duplex=duplex,
                mode=mode,
                untagged_vlan_vid=untagged_vid,
                tagged_vlan_vids=tagged_vids,
                poe_enabled=poe_enabled,
                poe_mode=poe_mode,
                enabled=bool(port.get("enable", not port.get("disable", False))),
                up=bool(port.get("up")),
            ))

        # Gateway uplink interfaces — not in port_table.
        # UniFi OS may expose them as wan1/wan2 sub-dicts, or only as an
        # uplink dict (name=WAN1/WAN2) with a separate wan2_ip field.
        built_wan_names: set[str] = set()

        for wan_key in ("wan1", "wan2"):
            wan = classic.get(wan_key)
            if wan and wan.get("ip"):
                label = wan_key.upper()
                built_wan_names.add(label)
                interfaces.append(CollectedInterface(
                    name=label,
                    speed=_speed_mbps_to_kbps(wan.get("speed")),
                    duplex="full" if wan.get("full_duplex") else None,
                    enabled=True,
                    up=True,
                    ip_address=wan.get("ip"),
                ))

        # Fallback: build from the uplink dict when wan1/wan2 sub-dicts are absent
        uplink = classic.get("uplink") or {}
        if uplink.get("name") and uplink["name"] not in built_wan_names:
            label = uplink["name"]
            built_wan_names.add(label)
            interfaces.append(CollectedInterface(
                name=label,
                speed=_speed_mbps_to_kbps(uplink.get("speed")),
                duplex="full" if uplink.get("full_duplex") else None,
                enabled=True,
                up=True,
                ip_address=uplink.get("ip"),
            ))
        # Second WAN when only wan2_ip is present (no wan2 sub-dict)
        if classic.get("wan2_ip") and "WAN2" not in built_wan_names:
            interfaces.append(CollectedInterface(
                name="WAN2",
                enabled=True,
                up=True,
                ip_address=classic.get("wan2_ip"),
            ))

        return interfaces

    # --- Network builder ----------------------------------------------------

    def _build_networks(
        self,
        int_networks: list[dict],
        netconf_by_id: dict[str, dict],
    ) -> list[CollectedNetwork]:
        """
        Classic networkconf is primary when available — it carries the subnet
        and DHCP range, which the Integration API list doesn't reliably
        include.  Integration API networks fill in anything classic doesn't
        have (matched by VLAN ID, then by name).
        """
        networks: list[CollectedNetwork] = []
        seen_vlans: set[int] = set()
        seen_names: set[str] = set()

        for nc in netconf_by_id.values():
            purpose = nc.get("purpose", "")
            if purpose == "wan":
                continue  # WAN networks aren't LAN subnets
            vid = _classic_vlan(nc)
            name = nc.get("name", "")
            networks.append(CollectedNetwork(
                name=name,
                purpose=purpose,
                vlan_id=vid,
                subnet=nc.get("ip_subnet"),
                dhcp_start=nc.get("dhcpd_start"),
                dhcp_stop=nc.get("dhcpd_stop"),
                netconf_id=nc.get("_id", ""),
            ))
            if vid:
                seen_vlans.add(vid)
            seen_names.add(name.lower())

        for n in (int_networks or []):
            vid = _to_int(n.get("vlanId"))
            name = n.get("name", "")
            if (vid and vid in seen_vlans) or name.lower() in seen_names:
                continue
            dhcp = n.get("dhcpRange") or {}
            networks.append(CollectedNetwork(
                name=name,
                purpose=n.get("purpose", "") or n.get("management", ""),
                vlan_id=vid if vid and vid > 1 else None,
                subnet=n.get("subnet"),
                dhcp_start=dhcp.get("from"),
                dhcp_stop=dhcp.get("to"),
                netconf_id=n.get("id", ""),
            ))

        return networks

    # --- WLAN builder -------------------------------------------------------

    def _build_wlans(
        self,
        classic_wlans: list[dict],
        int_wlans: list[dict],
        netconf_by_id: dict[str, dict],
    ) -> list[CollectedWlan]:
        """Classic wlanconf is preferred; Integration wifi_broadcasts fills gaps."""
        wlans: list[CollectedWlan] = []
        seen_names: set[str] = set()

        for w in classic_wlans:
            name = w.get("name", "")
            seen_names.add(name)
            vlan_id = _classic_vlan(w) if w.get("vlan_enabled") else None

            wlans.append(CollectedWlan(
                name=name,
                ssid=name,
                enabled=w.get("enabled", True),
                hidden=w.get("hide_ssid", False),
                band=w.get("band"),
                band_steering=w.get("band_steering_enabled", False),
                client_limit=w.get("client_limit_per_user"),
                vlan_id=vlan_id,
                security=w.get("security"),
            ))

        # Integration API fills anything not in classic
        for w in (int_wlans or []):
            name = w.get("name", "")
            if name in seen_names:
                continue
            wlans.append(CollectedWlan(
                name=name,
                ssid=w.get("ssid", name),
                enabled=w.get("enabled", True),
                hidden=w.get("hideSsid", False),
                band_steering=w.get("bandSteering", False),
                client_limit=w.get("clientLimit"),
            ))

        return wlans
