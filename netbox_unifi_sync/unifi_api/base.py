import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .exceptions import UnifiAPIError, UnifiAuthError, UnifiConnectionError, UnifiNotFound

USER_AGENT = "netbox-unifi-sync/0.1"


class BaseClient:
    def __init__(self, base_url, verify=True, timeout=30):
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.verify = verify
        self.session.headers.update({"Accept": "application/json", "User-Agent": USER_AGENT})
        if verify is False:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        # Retry only idempotent reads; login/POSTs are never retried automatically.
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=(429, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def _url(self, path):
        return f"{self.base_url}/{path.lstrip('/')}"

    def _request(self, method, path, **kwargs):
        kwargs.setdefault("timeout", self.timeout)
        try:
            return self.session.request(method, self._url(path), **kwargs)
        except requests.exceptions.SSLError as exc:
            raise UnifiConnectionError(
                f"TLS error talking to {self.base_url} -- set verify_ssl to a CA bundle path "
                f"or False for a self-signed cert: {exc}"
            ) from exc
        except requests.RequestException as exc:
            raise UnifiConnectionError(f"{method} {path} failed: {exc}") from exc

    @staticmethod
    def _json(resp):
        if resp.status_code in (401, 403):
            raise UnifiAuthError(f"{resp.status_code} on {resp.request.method} {resp.url}")
        if resp.status_code == 404:
            raise UnifiNotFound(f"404 on {resp.url}")
        if not resp.ok:
            raise UnifiAPIError(
                f"{resp.status_code} on {resp.request.method} {resp.url}",
                status=resp.status_code,
                body=resp.text[:500],
            )
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise UnifiAPIError(f"Non-JSON response from {resp.url}", status=resp.status_code,
                                body=resp.text[:500]) from exc

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
