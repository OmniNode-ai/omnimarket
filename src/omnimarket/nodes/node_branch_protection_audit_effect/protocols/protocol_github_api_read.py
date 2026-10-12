# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read-only GitHub boundary used to collect an audit snapshot."""

from typing import Protocol

from pydantic import JsonValue


class ProtocolGitHubApiRead(Protocol):
    """Read REST paths without a leading slash and GraphQL data payloads.

    Both operations raise ``omnimarket.github_api.GitHubApiError`` on failure.
    """

    def read_rest(self, path: str) -> JsonValue: ...

    def read_graphql(self, query: str, variables: dict[str, str]) -> JsonValue: ...
