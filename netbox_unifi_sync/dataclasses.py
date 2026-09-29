"""Vendor-neutral intermediate dataclasses.

The collector populates these from UniFi API responses.
The syncer writes them into NetBox.
Nothing here imports Django or NetBox — these are plain Python dataclasses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class CollectedDevice:
    # Identity
    name: str
    mac: str                        # Primary match key (macAddress from Integration API)
    serial: Optional[str] = None    # From classic stat/device
    unifi_device_id: str = ""       # Integration API UUID
    model: str = ""                 # e.g. "US 48 PoE 500W", "USG-Pro-4"
    device_type_slug: str = ""      # Informational only; the syncer keys DeviceTypes on model
    family: str = ""                # "switch", "gateway", "ap" or "other"

    # Status / firmware
    firmware: str = ""
    status: str = "active"          # "active" or "offline"

    # Network
    management_ip: Optional[str] = None   # e.g. "192.168.1.1"
    management_prefix_length: int = 24

    # Site (matched via unifi_site_id custom field)
    site_id: Optional[int] = None   # NetBox Site pk — resolved by syncer

    # GPS — inherited from NetBox Site, same pattern as Meraki
    latitude: Optional[float] = None
    longitude: Optional[float] = None

    # Uplink / WAN (gateways only)
    uplink_config: str = ""         # e.g. "WAN1:primary,WAN2:failover"

    # Interfaces collected for this device
    interfaces: list[CollectedInterface] = field(default_factory=list)


@dataclass
class CollectedInterface:
    name: str                               # e.g. "Port 1", "eth0", "WAN"
    port_idx: Optional[int] = None         # switch port index from port_table

    # Speed & duplex (built-in NetBox fields)
    speed: Optional[int] = None            # kbps (NetBox unit), e.g. 1000000 for 1G
    duplex: Optional[str] = None           # "full", "half", "auto"

    # VLAN assignment (built-in NetBox fields)
    mode: Optional[str] = None             # "access", "tagged", "tagged-all"
    untagged_vlan_vid: Optional[int] = None
    tagged_vlan_vids: list[int] = field(default_factory=list)

    # PoE (custom field on Interface, mirrors meraki_poe_enabled/meraki_poe_limit_w)
    poe_enabled: Optional[bool] = None
    poe_mode: Optional[str] = None         # "auto", "pasv24", "passthrough", "off"

    # Link state
    enabled: bool = True
    up: bool = False

    # IPv4 address on this interface (gateway WAN uplinks only)
    ip_address: Optional[str] = None


@dataclass
class CollectedNetwork:
    """One network/VLAN from rest/networkconf or Integration API /networks."""
    name: str
    purpose: str                    # "corporate", "vlan", "wan", "guest", "nat", "ppp"
    vlan_id: Optional[int] = None
    subnet: Optional[str] = None   # CIDR, e.g. "192.168.10.0/24"
    # DHCP range
    dhcp_start: Optional[str] = None
    dhcp_stop: Optional[str] = None
    # Internal IDs for portconf cross-reference
    netconf_id: str = ""            # classic _id field


@dataclass
class CollectedRoute:
    """Static route from rest/routing."""
    name: str
    network: str        # destination CIDR, e.g. "10.10.0.0/16"
    nexthop: str        # next-hop IP
    enabled: bool = True


@dataclass
class CollectedWlan:
    """SSID from rest/wlanconf (supplemented by Integration wifi_broadcasts)."""
    name: str
    ssid: str
    enabled: bool = True
    hidden: bool = False
    band: Optional[str] = None      # "2g", "5g", "6g", None means both
    band_steering: bool = False
    client_limit: Optional[int] = None
    vlan_id: Optional[int] = None
    security: Optional[str] = None  # classic: "open", "wpapsk", "wpaeap"


@dataclass
class CollectedSite:
    """One UniFi site with all its collected data."""
    unifi_site_id: str              # Integration API UUID
    internal_reference: str         # classic short name, e.g. "clqd72kz"
    name: str

    devices: list[CollectedDevice] = field(default_factory=list)
    networks: list[CollectedNetwork] = field(default_factory=list)
    routes: list[CollectedRoute] = field(default_factory=list)
    wlans: list[CollectedWlan] = field(default_factory=list)
