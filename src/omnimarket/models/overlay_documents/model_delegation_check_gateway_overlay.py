# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model the settings store validates the delegation check gateway row through (OMN-26642).

The row is where the delegation checks that used to run as cloud workflows find the system: the dev gateway's address and the
test tenant they act as. The address has no default and no value lives here: a row with no address does not load, and a
loopback or host-local address is refused, because a check that reached the gateway through the machine it runs on would not
be the customer path. The tenant's credential itself is not in this row: the row names its kind and, when the dev tenant node's
overlay does not, its secret name.
"""

from __future__ import annotations

import ipaddress
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.secret_store.model_secret_store_overlay import (
    ModelSecretStoreOverlay,
)

_HOST_LOCAL = frozenset(
    {"localhost", "host.docker.internal", "gateway.docker.internal"}
)


class ModelDelegationCheckGatewayOverlay(BaseModel):
    """The gateway address and the test tenant of the gateway-graded delegation checks; no key has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    gateway_url: str = Field(min_length=1)
    # A tenant slug the dev tenant node's overlay declares; the token is read by that node's secret name.
    tenant_slug: str = Field(min_length=1)
    # How the gateway reads the tenant's credential: an API key travels in the x-api-key header and is resolved by its digest,
    # a signed JWT travels in the Authorization header as a bearer token. The kind has no default: it is the row's to say.
    credential_kind: Literal["api_key", "jwt"]
    # The secret name of the tenant's credential in the dev secret store; with none the dev tenant node's overlay names it.
    secret_name: str | None = Field(default=None, min_length=1)

    @field_validator("gateway_url")
    @classmethod
    def _customer_path_address(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError("gateway_url is an http or https base URL with a host")
        if parsed.port is None:
            raise ValueError("gateway_url names an explicit port")
        if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError(
                "gateway_url is a base URL with no path, query or fragment"
            )
        host = parsed.hostname.lower()
        if host in _HOST_LOCAL:
            raise ValueError("gateway_url names a host-local alias")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and address.is_loopback:
            raise ValueError("gateway_url names a loopback address")
        return f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"


class ModelDevTenantTokenRow(BaseModel):
    """One tenant row of the dev tenant node's overlay, read for its slug and the secret name of its token only."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    tenant_slug: str = Field(min_length=1)
    secret_name: str = Field(min_length=1)


class ModelDevTenantTokenSource(BaseModel):
    """The part of the dev tenant node's overlay the checks read: where its tokens sit and the tenants it declares."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    store: ModelSecretStoreOverlay
    token_folder: str = Field(pattern=r"^/")
    tenants: list[ModelDevTenantTokenRow] = Field(min_length=1)


__all__ = [
    "ModelDelegationCheckGatewayOverlay",
    "ModelDevTenantTokenRow",
    "ModelDevTenantTokenSource",
]
