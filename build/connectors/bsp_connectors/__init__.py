"""DataPlatform enterprise connectors — one SDK, each system mock-or-live.

    from bsp_connectors import m365, atlan, bigquery, powerbi, sap_brim, mode_of
    mode_of("m365")            # "mock" until M365_TENANT_ID/CLIENT_ID/CLIENT_SECRET are set

The stable surface is plan/architecture.md §4.1. Submodules load lazily so
importing the package never pulls google-cloud / msal / duckdb until used.
`sap_brim` is owned by build/disputes; when it is not installed yet, the
name still imports but any use raises a clear ConnectorError.
"""

from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any

from ._common import ConnectorError, NotConfigured, NotFound, mode_of

SYSTEMS = ("m365", "atlan", "bigquery", "powerbi", "sap_brim")

__all__ = ["m365", "atlan", "bigquery", "powerbi", "sap_brim", "mode_of", "SYSTEMS",
           "ConnectorError", "NotFound", "NotConfigured"]


class _Unavailable(ModuleType):
    """Placeholder for a system package that failed to import."""

    def __init__(self, name: str, error: BaseException):
        super().__init__(f"bsp_connectors.{name}")
        self._name, self._error = name, error

    def __getattr__(self, attr: str) -> Any:
        if attr.startswith("__"):
            raise AttributeError(attr)
        raise ConnectorError(
            f"bsp_connectors.{self._name} is not available ({type(self._error).__name__}: {self._error}). "
            f"It is provided by build/{'disputes' if self._name == 'sap_brim' else 'connectors'} — "
            f"check that bsp_connectors/{self._name}/ exists and its dependencies are installed.",
            system=self._name)

    def __bool__(self) -> bool:
        return False


def __getattr__(name: str) -> Any:
    if name not in SYSTEMS:
        raise AttributeError(f"module 'bsp_connectors' has no attribute {name!r}")
    try:
        mod = importlib.import_module(f".{name}", __name__)
    except ImportError as e:
        if name != "sap_brim":
            raise
        mod = _Unavailable(name, e)  # not cached: picked up as soon as build/disputes ships it
        return mod
    globals()[name] = mod
    return mod
