# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the caller tenant of a request served through a runtime HTTP mount.

The hosted edge supplies the implementation. A mount that needs a tenant and has
no resolver bound refuses every request; it never serves one unauthenticated.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from omnimarket.models.model_http_tenant_identity import ModelHttpTenantIdentity


@runtime_checkable
class ProtocolHttpTenantIdentity(Protocol):
    """Resolve the authenticated caller tenant from the request headers."""

    async def resolve(
        self, headers: Mapping[str, str]
    ) -> ModelHttpTenantIdentity | None: ...


__all__: list[str] = ["ProtocolHttpTenantIdentity"]
