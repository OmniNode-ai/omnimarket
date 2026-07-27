# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_github_repo_gateway_effect — typed GitHub repo status reader (read-only).

First slice: a discriminated-union read gateway that returns one small typed
object per operation, replacing raw ``gh`` JSON dumps in verify loops. Reuses the
existing GitHub GraphQL/REST transport; it does not mutate GitHub state.
"""

from omnimarket.nodes.node_github_repo_gateway_effect.handlers.handler_github_repo_gateway import (
    HandlerGithubRepoGatewayEffect,
)

__all__ = ["HandlerGithubRepoGatewayEffect"]
