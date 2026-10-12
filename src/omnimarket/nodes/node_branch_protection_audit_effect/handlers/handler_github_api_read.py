# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""GitHub reads with a lazily resolved, contract-declared token."""

from pathlib import Path
from typing import cast

from pydantic import JsonValue, SecretStr

from omnimarket import github_api
from omnimarket.inference.secret_store_resolver import resolve_api_key
from omnimarket.nodes.contract_topics import contract_secret_ref
from omnimarket.nodes.node_branch_protection_audit_effect.protocols.protocol_github_api_read import (
    ProtocolGitHubApiRead,
)

_CONTRACT_PATH = Path(__file__).resolve().parents[1] / "contract.yaml"


class GitHubTokenResolutionError(github_api.GitHubApiError):
    """The audit cannot start because its token reference is unresolved."""


class HandlerGitHubApiRead(ProtocolGitHubApiRead):
    """Use the shared HTTP helpers, resolving the token only on first use."""

    def __init__(self) -> None:
        self._secret: SecretStr | None = None

    def _token(self) -> str:
        if self._secret is None:
            ref = contract_secret_ref(_CONTRACT_PATH, "GITHUB_TOKEN")
            try:
                secret = resolve_api_key(ref)
            except Exception:
                # The resolver's exception may contain sensitive backend text.
                raise GitHubTokenResolutionError(
                    f"could not resolve api_key_ref {ref!r}"
                ) from None
            if secret is None:
                raise GitHubTokenResolutionError(
                    f"api_key_ref {ref!r} resolved to None"
                )
            self._secret = secret
        return self._secret.get_secret_value()

    def read_rest(self, path: str) -> JsonValue:
        token = self._token()
        if path.endswith("/rulesets"):
            return cast(
                JsonValue, github_api.rest_json_array("GET", f"/{path}", token=token)
            )
        return cast(JsonValue, github_api.rest_json("GET", f"/{path}", token=token))

    def read_graphql(self, query: str, variables: dict[str, str]) -> JsonValue:
        return cast(
            JsonValue, github_api.graphql(query, dict(variables), token=self._token())
        )
