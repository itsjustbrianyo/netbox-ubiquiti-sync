class UnifiError(Exception):
    """Base class for all UniFi client errors."""


class UnifiConnectionError(UnifiError):
    """Network/TLS failure reaching the controller."""


class UnifiAuthError(UnifiError):
    """401/403 or rejected login."""


class UnifiNotFound(UnifiError):
    """404 -- often means the endpoint doesn't exist on this Network version."""


class UnifiAPIError(UnifiError):
    def __init__(self, message, status=None, body=None):
        super().__init__(message)
        self.status = status
        self.body = body
