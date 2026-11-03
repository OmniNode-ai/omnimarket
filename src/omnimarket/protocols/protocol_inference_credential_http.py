# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports the BYOK inference-credential HTTP routes reach their world through."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ProtocolInferenceCredentialReadModel(Protocol):
    """Read side of the ``tenant_inference_credentials`` projection.

    Errors are typed: ``CredentialReadModelMissingTableError`` for an absent
    relation, ``CredentialReadModelNotConfiguredError`` for an unset source,
    ``CredentialReadModelUnavailableError`` for anything else.
    """

    async def list_for_tenant(self, tenant_id: str) -> list[Mapping[str, Any]]: ...

    async def owned_by_tenant(
        self, api_key_ref: str, tenant_id: str
    ) -> Mapping[str, Any] | None: ...


@runtime_checkable
class ProtocolSecretValueSource(Protocol):
    """Resolve one secret by its logical name; ``None`` when it is not mapped."""

    async def get(self, logical_name: str) -> str | None: ...


@runtime_checkable
class ProtocolRevokeOutbox(Protocol):
    """Record a revoke request before the store delete (T18a section 7)."""

    async def record(self, *, api_key_ref: str, tenant_id: str) -> None: ...


__all__: list[str] = [
    "ProtocolInferenceCredentialReadModel",
    "ProtocolRevokeOutbox",
    "ProtocolSecretValueSource",
]
