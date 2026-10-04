"""Typed error hierarchy surfaced to the UI with actionable messages."""


class PnPBridgeError(Exception):
    """Base class; `message` is safe to show to the user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ConfigurationError(PnPBridgeError):
    """Missing or invalid configuration (e.g. credentials not set)."""


class CatalystError(PnPBridgeError):
    """Catalyst Center returned an unexpected error."""


class CatalystAuthError(CatalystError):
    """Authentication against Catalyst Center failed."""


class CatalystTransientError(CatalystError):
    """Catalyst Center is temporarily unavailable (unreachable, HTTP 429 or 5xx).

    A long-running poll keeps going on this instead of failing the device:
    CCC is still working on the claim/deploy while it is briefly unreachable
    or rate limiting."""


class NetBoxError(PnPBridgeError):
    """NetBox returned an unexpected error."""


class NetBoxAuthError(NetBoxError):
    """Authentication against NetBox failed."""


class NetBoxNotFound(NetBoxError):
    """A NetBox object was not found."""


class TaskTimeout(CatalystError):
    """A Catalyst Center task did not finish within the allowed time."""
