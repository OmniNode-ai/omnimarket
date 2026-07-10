# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared GitHub-token resolution for node_github_repo_gateway_effect.

Every read in this node resolves the GitHub token through this single boundary:
the ref NAME comes from the contract ``secrets`` block (never a bare literal),
and the VALUE comes from the canonical secret-store resolver — never from a raw
process-environment read, and the value is never logged.
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.inference.secret_store_resolver import (
    resolve_api_key,
    resolve_api_key_async,
)
from omnimarket.nodes.contract_topics import contract_secret_ref

_CONTRACT_PATH = Path(__file__).resolve().parent / "contract.yaml"
_SECRET_NAME = "GITHUB_TOKEN"


def resolve_github_token() -> str:
    """Resolve the GitHub token value synchronously (CLI boundary)."""
    ref = contract_secret_ref(_CONTRACT_PATH, _SECRET_NAME)
    secret = resolve_api_key(ref)
    if secret is None:
        raise RuntimeError(
            f"api_key_ref {ref!r} resolved to None — "
            f"ensure {_SECRET_NAME} is set in the secret store."
        )
    return secret.get_secret_value()


async def resolve_github_token_async() -> str:
    """Resolve the GitHub token value from an async effect boundary."""
    ref = contract_secret_ref(_CONTRACT_PATH, _SECRET_NAME)
    secret = await resolve_api_key_async(ref)
    if secret is None:
        raise RuntimeError(
            f"api_key_ref {ref!r} resolved to None — "
            f"ensure {_SECRET_NAME} is set in the secret store."
        )
    return secret.get_secret_value()


__all__: list[str] = ["resolve_github_token", "resolve_github_token_async"]
