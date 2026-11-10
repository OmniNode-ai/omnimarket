# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The write side of the routes: the value-to-ref exchange and the revocation."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.projection.credential_publisher import (
    ModelInferenceCredentialCreateRequest,
    ModelInferenceCredentialResponse,
    ModelInferenceCredentialRevokeResponse,
)


@runtime_checkable
class ProtocolInferenceCredentialIntake(Protocol):
    """Store a customer's key and publish its event; delete it and publish the revocation.

    The deployed binding calls omnimarket's credential publisher. Neither call checks
    that the ref belongs to the tenant: the handler resolves ownership first.
    """

    async def register(
        self, request: ModelInferenceCredentialCreateRequest, *, tenant_id: str
    ) -> ModelInferenceCredentialResponse: ...

    async def revoke(
        self, api_key_ref: str, *, tenant_id: str
    ) -> ModelInferenceCredentialRevokeResponse: ...


__all__: list[str] = ["ProtocolInferenceCredentialIntake"]
