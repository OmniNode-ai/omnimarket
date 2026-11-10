# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The read model the list route and the revoke ownership check read."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_inference_credentials.models.model_inference_credential_list_item import (
    ModelInferenceCredentialListItem,
)


class CredentialReadModelAbsentError(RuntimeError):
    """The tenant_inference_credentials relation does not exist on this plane yet."""


class CredentialReadModelNotConfiguredError(RuntimeError):
    """No projection database binding is configured for this runtime."""


class CredentialReadModelUnavailableError(RuntimeError):
    """The database is configured and unreachable, or the read errored."""


@runtime_checkable
class ProtocolInferenceCredentialReadModel(Protocol):
    """Read one tenant's credential refs from the projection the writer materializes.

    Raises CredentialReadModelAbsentError, CredentialReadModelNotConfiguredError or
    CredentialReadModelUnavailableError; never answers an empty result for a failure.
    """

    async def list_for_tenant(
        self, tenant_id: str
    ) -> Sequence[ModelInferenceCredentialListItem]: ...

    async def owned_by_tenant(
        self, api_key_ref: str, tenant_id: str
    ) -> ModelInferenceCredentialListItem | None:
        """The row iff the ref exists and ``tenant_id`` owns it; None otherwise."""
        ...


__all__: list[str] = [
    "CredentialReadModelAbsentError",
    "CredentialReadModelNotConfiguredError",
    "CredentialReadModelUnavailableError",
    "ProtocolInferenceCredentialReadModel",
]
