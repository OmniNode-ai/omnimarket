# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Linear project tracker the ticketing nodes read tickets through (OMN-21676).

``node_create_ticket`` builds ``AdapterLinearGraphQLProjectTracker`` from the key its own
contract declares, resolved through the secret store at the effect boundary (Infisical on the
lab). A node that only needs to read a ticket's body takes the same tracker the same way, so
no handler reads a Linear credential from the environment by name and none opens its own
GraphQL connection.

The credential is the caller node's contract-declared ``LINEAR_API_KEY`` ref. It is resolved
here, handed to the adapter and never returned, logged or placed in an error message.
"""

from __future__ import annotations

from pathlib import Path

from omnibase_infra.adapters.project_tracker.linear_graphql_project_tracker_adapter import (
    AdapterLinearGraphQLProjectTracker,
)

from omnimarket.inference.secret_store_resolver import resolve_api_key_async
from omnimarket.nodes.contract_topics import contract_secret_ref

__all__ = ["LINEAR_SECRET_NAME", "linear_project_tracker"]

LINEAR_SECRET_NAME = "LINEAR_API_KEY"


async def linear_project_tracker(
    contract_path: Path, *, secret_name: str = LINEAR_SECRET_NAME
) -> AdapterLinearGraphQLProjectTracker:
    """The Linear tracker keyed by the secret ``contract_path`` declares.

    Raises ``ValueError`` when the contract does not declare the secret and
    ``RuntimeError`` when the secret store holds no value for it. The caller closes the
    tracker.
    """
    ref = contract_secret_ref(contract_path, secret_name)
    secret = await resolve_api_key_async(ref, env_var_fallback=ref)
    if secret is None:
        raise RuntimeError(
            f"api_key_ref {ref!r} resolved to None; set it in the secret store"
        )
    return AdapterLinearGraphQLProjectTracker(api_key=secret.get_secret_value())
