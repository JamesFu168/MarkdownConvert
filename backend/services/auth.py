"""Caller identity.

* ``easyauth``: App Service Authentication (Microsoft Entra ID) injects
  ``X-MS-CLIENT-PRINCIPAL-ID`` / ``X-MS-CLIENT-PRINCIPAL-NAME``; the platform strips
  these headers from external requests, so they can be trusted only when Easy Auth is ON.
* ``local_dev``: loopback-only fixed identity for local development. It is refused when the
  process runs on Azure (``WEBSITE_INSTANCE_ID`` present) and when the Host header is not
  a loopback address. The browser can never choose the owner id.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from domain.errors import ForbiddenError, ProviderConfigurationError, UnauthorizedError
from services.config import Settings

LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]", "::1")


@dataclass(frozen=True)
class Principal:
    owner_id: str
    display_name: str | None
    mode: str


def _host_is_loopback(host: str | None) -> bool:
    if not host:
        return False
    h = host.strip().lower()
    if h.startswith("["):
        h = h.split("]")[0] + "]"
    else:
        h = h.split(":")[0]
    return h in LOOPBACK_HOSTS


def resolve_principal(headers: Mapping[str, str], settings: Settings) -> Principal:
    mode = settings.auth_mode
    if mode == "local_dev":
        if settings.running_on_azure:
            raise ProviderConfigurationError("AUTH_MODE=local_dev is not allowed on Azure; set AUTH_MODE=easyauth")
        if not _host_is_loopback(headers.get("host") or headers.get("Host")):
            raise ForbiddenError("local_dev identity is only available on loopback requests")
        if headers.get("x-forwarded-for") or headers.get("X-Forwarded-For"):
            raise ForbiddenError("local_dev identity is not available for forwarded requests")
        return Principal(owner_id=settings.local_dev_user_id, display_name="本機開發者", mode=mode)
    if mode == "easyauth":
        pid = headers.get("x-ms-client-principal-id") or headers.get("X-MS-CLIENT-PRINCIPAL-ID")
        if not pid:
            raise UnauthorizedError("Missing authenticated principal (App Service Authentication required)")
        name = headers.get("x-ms-client-principal-name") or headers.get("X-MS-CLIENT-PRINCIPAL-NAME")
        return Principal(owner_id=pid, display_name=name, mode=mode)
    raise ProviderConfigurationError(f"Unknown AUTH_MODE {mode!r}")
