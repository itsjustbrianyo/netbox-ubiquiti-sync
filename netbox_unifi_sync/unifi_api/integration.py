"""Official UniFi Network Integration API (API key auth).

UniFi OS path: /proxy/network/integration/v1/...
"""
from urllib.parse import quote

from .base import BaseClient

PREFIX = "proxy/network/integration/v1"


class IntegrationClient(BaseClient):
    def __init__(self, base_url, api_key, page_size=100, **kwargs):
        super().__init__(base_url, **kwargs)
        if not api_key:
            raise ValueError("api_key is required for the Integration API")
        self.session.headers["X-API-KEY"] = api_key
        self.page_size = page_size

    def get(self, path, params=None):
        return self._json(self._request("GET", f"{PREFIX}/{path.lstrip('/')}", params=params))

    def paginate(self, path, params=None):
        """Yield items across offset/limit pages ({offset, limit, count, totalCount, data})."""
        offset = 0
        while True:
            page = dict(params or {}, offset=offset, limit=self.page_size)
            body = self.get(path, page)
            if isinstance(body, list):
                yield from body
                return
            data = (body or {}).get("data", [])
            yield from data
            offset += len(data)
            total = (body or {}).get("totalCount")
            if not data:
                return
            if total is not None and offset >= total:
                return
            if total is None and len(data) < self.page_size:
                return

    # --- resources -----------------------------------------------------------
    def info(self):
        return self.get("info")

    def sites(self):
        return list(self.paginate("sites"))

    def integration_devices(self, site_id):
        return list(self.paginate(f"sites/{quote(site_id)}/devices"))

    # Keep the old name too so unifi_list_sites management command still works
    def devices(self, site_id):
        return self.integration_devices(site_id)

    def device(self, site_id, device_id):
        return self.get(f"sites/{quote(site_id)}/devices/{quote(device_id)}")

    def networks(self, site_id):
        return list(self.paginate(f"sites/{quote(site_id)}/networks"))

    def wifi_broadcasts(self, site_id):
        return list(self.paginate(f"sites/{quote(site_id)}/wifi/broadcasts"))
