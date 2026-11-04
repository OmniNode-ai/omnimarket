# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ticket read's credential: resolved from the secret store, in process (OMN-22418).

The project tracker adapter reads ``LINEAR_API_KEY`` from the environment when
it is built without a key. node_ticket_query never builds it that way: the key
is one ``resolve`` request to node_secret_store_effect, and the adapter is
handed the value. The reference is declared in this node's ``contract.yaml``.
A key the store does not hold is a typed refusal naming the reference; there is
no environment fallback and no key file.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

import httpx
import yaml
from omnibase_infra.adapters.project_tracker.linear_graphql_project_tracker_adapter import (
    AdapterLinearGraphQLProjectTracker,
)

from omnimarket.config.service_endpoints import service_endpoint_url
from omnimarket.models.secret_store.model_secret_store_request import (
    EnumSecretStoreOperation,
    ModelSecretStoreRequest,
)
from omnimarket.models.secret_store.model_secret_store_result import (
    EnumSecretStoreOutcome,
    ModelSecretStoreResult,
)
from omnimarket.models.ticket_query.model_ticket_credential_refusal import (
    TicketQueryCredentialRefusedError,
)

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
_SECRET_NAME = "tracker_api_key"


class ProtocolGraphQLPoster(Protocol):
    """The adapter's single GraphQL POST."""

    async def post_graphql(
        self, query: str, variables: dict[str, object] | None = None
    ) -> httpx.Response:
        """POST one query."""
        ...


class ProtocolSecretResolve(Protocol):
    """The one call made on node_secret_store_effect's handler."""

    async def handle(self, request: ModelSecretStoreRequest) -> ModelSecretStoreResult:
        """Answer one resolve request."""
        ...


def linear_secret_reference() -> str:
    """The ``secret://`` reference of the tracker key, built from the contract's declaration."""
    raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    declared = raw["secrets"][_SECRET_NAME]
    folder = str(declared["store_folder"]).strip("/")
    return f"{service_endpoint_url('internal_targets.secret_reference_prefix')}{folder}/{declared['key']}"


def _build_adapter(api_key: str) -> AdapterLinearGraphQLProjectTracker:
    return AdapterLinearGraphQLProjectTracker(api_key=api_key)


async def resolve_linear_tracker(
    secret_node: ProtocolSecretResolve,
    tracker_factory: Callable[[str], ProtocolGraphQLPoster] = _build_adapter,
) -> ProtocolGraphQLPoster:
    """Resolve the key from the store and build the tracker over it.

    Raises:
        TicketQueryCredentialRefusedError: the store did not resolve the key.
    """
    reference = linear_secret_reference()
    result = await secret_node.handle(
        ModelSecretStoreRequest(
            operation=EnumSecretStoreOperation.RESOLVE, reference=reference
        )
    )
    if result.outcome is not EnumSecretStoreOutcome.RESOLVED:
        raise TicketQueryCredentialRefusedError(
            reference, result.outcome.value, result.detail or ""
        )
    api_key = (
        result.value.get_secret_value().strip() if result.value is not None else ""
    )
    if not api_key:
        raise TicketQueryCredentialRefusedError(
            reference,
            EnumSecretStoreOutcome.VALUE_MISSING.value,
            "the store returned no usable tracker credential",
        )
    return tracker_factory(api_key)


__all__ = [
    "ProtocolGraphQLPoster",
    "ProtocolSecretResolve",
    "linear_secret_reference",
    "resolve_linear_tracker",
]
