# netbox-unifi-sync

Syncs a self-hosted UniFi OS controller into NetBox. It is built to work the
same way as `netbox-meraki-sync`: same config style, same one management
command, same SyncLog page, same custom script, same changelog attribution.

Python package / app name: **`netbox_unifi_sync`** (pip name `netbox-unifi-sync`).

## What gets synced

| UniFi | NetBox |
|---|---|
| Devices (switches, gateways, APs) | `dcim.Device` with role, type, serial, firmware, lat/long and tenant inherited from the Site |
| Switch / gateway ports | `dcim.Interface` with speed, duplex, mode, untagged + tagged VLANs, PoE custom fields |
| AP radio | `wifi0` interface (SSIDs attach to it) |
| Device base MAC | `dcim.MACAddress` on the first non-WAN interface |
| Management IP, gateway WAN IPs | `ipam.IPAddress` (WAN as /32), primary IPv4 set |
| Networks | `ipam.VLAN` (in `"{site} - UniFi - VLANs"`), `ipam.Prefix`, `ipam.IPRange`, gateway IP |
| Static routes | `ipam.Prefix` + `ipam.IPRange` with the next hop in the description |
| SSIDs | `wireless.WirelessLAN` in `"{site} - UniFi - WLANs"` |

Everything it creates is tagged `unifi` plus whatever tags the Site has,
gets the Site's tenant, and lives in a per-site VRF named `"{site} - UniFi - VRF"`.

## Install

```bash
source /opt/netbox/venv/bin/activate
pip install git+https://github.com/<you>/<repo>.git
```

`configuration.py`:

```python
PLUGINS = ["netbox_unifi_sync"]            # add to the existing list

PLUGINS_CONFIG = {
    "netbox_unifi_sync": {
        "controller_url": "https://unifi.example.com",   # UniFi OS console, no path
        "api_key": "your-integration-api-key",           # Network > Settings > Integrations

        # Optional, but without these there are no serials, firmware,
        # port VLAN/PoE settings or static routes:
        "username": "readonly-local-account",
        "password": "its-password",

        # Optional
        "verify_ssl": True,                    # True, False, or path to a CA bundle
        "request_timeout": 30,
        "default_device_role": "network",
        "changelog_username": "unifi-sync",
        "enable_devicetype_library": False,    # enrich new DeviceTypes from GitHub
        "devicetype_library_cache_days": 7,
    },
}
```

Then:

```bash
cd /opt/netbox/netbox
python manage.py migrate netbox_unifi_sync      # netbox_unifi_sync, not the repo name
sudo systemctl restart netbox netbox-rq
```

The site custom fields are created automatically when NetBox starts; device and
interface custom fields are created on first sync. There is no setup command.

The classic-API account must be a **local** UniFi OS user (no SSO / MFA).
Read-only is enough.

## Use

1. See what the controller has:

   ```bash
   python manage.py sync_unifi --list-sites
   ```

2. On each NetBox Site, set the **UniFi Site ID** custom field to the site's
   UUID, internal reference (e.g. `clqd72kz`) or exact name.

3. Sync:

   ```bash
   python manage.py sync_unifi                    # all mapped sites
   python manage.py sync_unifi --site clqd72kz    # one site (value from the custom field)
   python manage.py sync_unifi --dry-run          # writes nothing
   python manage.py sync_unifi --user someone     # changelog attribution
   python manage.py sync_unifi --backfill-device-types
   ```

Cron:

```
0 */4 * * * netbox /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py sync_unifi >> /var/log/netbox/unifi_sync.log 2>&1
```

Or upload `scripts/unifi_sync_script.py` under **Customization → Scripts** to run
or schedule it from the UI. Results are under **UniFi Sync → Sync Logs**
(`/plugins/unifi/sync-logs/`, API at `/api/plugins/unifi/sync-logs/`).

## Sharing a site with the Meraki plugin

Both plugins can sync into the same NetBox Site, and they don't share any
containers: this plugin's VRF, VLAN group and WLAN group all have "UniFi" in the
name (`{site} - UniFi - VRF`, `- UniFi - VLANs`, `- UniFi - WLANs`), so its VLANs,
prefixes, IP ranges and SSIDs stay separate from Meraki's `{site} VRF` /
`{site} VLANs` / `{site} WLANs`. The same VLAN ID or subnet can therefore exist
once per vendor.

- Objects found outside those containers (created by hand, say) are only
  overwritten if they carry this plugin's `unifi` tag; otherwise they are
  gap-filled and never renamed or re-tagged.
- VLAN lookups (port VLANs, SSID VLANs) only look inside the UniFi VLAN group.
- AP radios are called `wifi0` (Meraki uses `Radio 0`), so SSIDs are never
  attached to the other vendor's APs.
- A device is never matched by name if it is tagged `meraki`. If a UniFi and a
  Meraki device have the same name on the same site, NetBox itself forbids the
  duplicate; the sync reports it as a device error and carries on. Rename one.
- Upgrading from 0.2.0 (which used `{site} VRF` etc. and a `src:unifi` tag): on first use the
  tag is renamed to `unifi` in place, and each old container is renamed in place, but only if it is non-empty and everything in it
  is tagged `unifi`. A container that also holds other objects is left alone
  and a fresh one is created; delete the leftover `unifi`-tagged objects from it by hand.

## Behaviour notes (same as the Meraki plugin unless stated)

- No stale-device flagging or deletion.
- Counters for SSIDs and static routes are re-counted every run.
- A changed IP (WAN or management) adds the new address; the old record is not removed.
- Differences from Meraki: interface speed is written in NetBox's kbps unit,
  and dry runs write nothing at all (not even the manufacturer, device type or tag).
- Gateway per-VLAN IPs are created but only attached to an interface if one
  named `LAN` / `VLAN{n}` already exists on the gateway.
- Device Type Library lookups use the unauthenticated GitHub API (60 requests
  per hour per IP); the index is cached for `devicetype_library_cache_days`.
  Only models the library has are enriched, and only some have images.

## Testing status

Run against a real NetBox 4.7.1 (Postgres, Redis) alongside the real Meraki
plugin, using a mock UniFi controller: dry run, full sync, idempotent re-run,
update propagation, Integration-only mode, error paths, changelog attribution,
web UI, REST API, UI script, and the device-type backfill with real library
downloads. It has **not** been run against a real UniFi controller: field names
in the Integration and Classic API responses were written from the API
documentation and probe summaries, so check the first `--dry-run` against your
real site. `scripts/unifi_probe.py` dumps the raw API responses if a field
looks wrong.
