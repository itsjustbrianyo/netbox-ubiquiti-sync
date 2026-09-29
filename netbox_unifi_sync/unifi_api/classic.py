"""Classic (internal) UniFi Network API via UniFi OS session auth.

Login:    POST /api/auth/login  -> TOKEN cookie + X-CSRF-Token header
Requests: /proxy/network/api/s/{site}/...   (site = short internal name, e.g. "default")

Requires a *local* UniFi OS account (UI.com SSO / MFA accounts can't log in here).
UniFi OS rate-limits logins, so the session is reused and only re-established on 401.
"""
from urllib.parse import quote

from .base import BaseClient
from .exceptions import UnifiAPIError, UnifiAuthError

NET = "proxy/network"


class ClassicClient(BaseClient):
    def __init__(self, base_url, username, password, **kwargs):
        super().__init__(base_url, **kwargs)
        if not (username and password):
            raise ValueError("username and password are required for the classic API")
        self._username = username
        self._password = password
        self._logged_in = False

    # --- session ---------------------------------------------------------
    def _update_csrf(self, resp):
        token = resp.headers.get("X-Updated-CSRF-Token") or resp.headers.get("X-CSRF-Token")
        if token:
            self.session.headers["X-CSRF-Token"] = token

    def login(self):
        resp = self._request(
            "POST", "api/auth/login",
            json={"username": self._username, "password": self._password, "rememberMe": False},
        )
        if resp.status_code in (401, 403):
            raise UnifiAuthError(
                "Classic login rejected -- confirm this is a local UniFi OS account "
                "without SSO/MFA and that it has at least read-only Network access"
            )
        if resp.status_code == 429:
            raise UnifiAuthError("Classic login rate-limited by UniFi OS -- wait and retry")
        self._json(resp)
        self._update_csrf(resp)
        self._logged_in = True

    def logout(self):
        if not self._logged_in:
            return
        try:
            self._request("POST", "api/auth/logout")
        except Exception:
            pass
        self._logged_in = False

    def close(self):
        self.logout()
        super().close()

    def _call(self, method, path, **kwargs):
        if not self._logged_in:
            self.login()
        resp = self._request(method, f"{NET}/{path.lstrip('/')}", **kwargs)
        if resp.status_code == 401:  # session expired -- one re-login attempt
            self._logged_in = False
            self.login()
            resp = self._request(method, f"{NET}/{path.lstrip('/')}", **kwargs)
        self._update_csrf(resp)
        body = self._json(resp)
        if isinstance(body, dict):
            meta = body.get("meta") or {}
            if meta.get("rc") not in (None, "ok"):
                raise UnifiAPIError(f"{path}: {meta.get('msg', 'rc=' + str(meta.get('rc')))}",
                                    status=resp.status_code, body=meta)
            return body.get("data", body)
        return body

    @staticmethod
    def _s(site):
        return f"api/s/{quote(site)}"

    # --- resources -------------------------------------------------------
    def sites(self):
        return self._call("GET", "api/self/sites")

    def devices(self, site):
        """Full device objects incl. port_table, uplink, lldp_table, serial (firmware-dependent)."""
        return self._call("GET", f"{self._s(site)}/stat/device")

    def networkconf(self, site):
        return self._call("GET", f"{self._s(site)}/rest/networkconf")

    def wlanconf(self, site):
        return self._call("GET", f"{self._s(site)}/rest/wlanconf")

    def portconf(self, site):
        """Switch port profiles referenced by port_overrides[].portconf_id."""
        return self._call("GET", f"{self._s(site)}/rest/portconf")

    def routing(self, site):
        """User-defined static routes."""
        return self._call("GET", f"{self._s(site)}/rest/routing")
