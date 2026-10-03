# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local dashboard's bind, request and result (OMN-19976, plan T2.3)."""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

# The standalone projection API (``projection/api_server.py``) and OmniDash's
# Express bridge own this port; the local dashboard never takes it.
_STANDALONE_API_PORT = 3002
_DEFAULT_HOST = "127.0.0.1"
_LOOPBACK_NAMES = frozenset({"localhost"})


class DashboardBindError(ValueError):
    """``dashboard.bind`` names a value the local dashboard must not bind."""


class ModelLocalDashboardServeRequest(BaseModel):
    """Serve this install's exposures on ``host:port`` for ``tenant_id``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str = Field(min_length=1, description="A loopback interface.")
    port: int = Field(ge=1, le=65535, description="Never the standalone API's port.")
    tenant_id: str | None = Field(
        default=None, description="The identity `onex local init` minted, if any."
    )

    @model_validator(mode="after")
    def _loopback_and_not_the_standalone_port(self) -> ModelLocalDashboardServeRequest:
        resolve_dashboard_bind({"dashboard": {"bind": f"{self.host}:{self.port}"}})
        return self


class ModelLocalDashboardServeResult(BaseModel):
    """What the serving process served before it stopped."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    exposure_count: int = Field(ge=0)
    tenant_id: str | None = None


def resolve_dashboard_bind(overlay: Mapping[str, object] | None) -> tuple[str, int]:
    """``(host, port)`` from the overlay's ``dashboard.bind``, or loopback and a free port.

    Raises :class:`DashboardBindError` for a malformed value, a non-loopback
    host, or the standalone API's port.
    """
    section = overlay.get("dashboard") if overlay else None
    bind = section.get("bind") if isinstance(section, Mapping) else None
    if bind is None:
        return _DEFAULT_HOST, 0
    if not isinstance(bind, str) or ":" not in bind:
        raise DashboardBindError(f"dashboard.bind must be host:port, got {bind!r}")
    host, _, port_text = bind.rpartition(":")
    host = host.strip("[]")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise DashboardBindError(
            f"dashboard.bind port is not a number: {bind!r}"
        ) from exc
    if not 0 <= port <= 65535:
        raise DashboardBindError(f"dashboard.bind port is out of range: {bind!r}")
    if port == _STANDALONE_API_PORT:
        raise DashboardBindError(
            f"dashboard.bind may not use port {_STANDALONE_API_PORT}: the standalone "
            "projection API and the OmniDash bridge own it"
        )
    if host not in _LOOPBACK_NAMES:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError as exc:
            raise DashboardBindError(
                f"dashboard.bind host is not an address: {bind!r}"
            ) from exc
        if not loopback:
            raise DashboardBindError(
                f"dashboard.bind must name a loopback interface, got {host!r}"
            )
    return host, port


__all__ = [
    "DashboardBindError",
    "ModelLocalDashboardServeRequest",
    "ModelLocalDashboardServeResult",
    "resolve_dashboard_bind",
]
