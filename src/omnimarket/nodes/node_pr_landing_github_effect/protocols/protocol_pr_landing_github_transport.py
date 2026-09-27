# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Transport seam of node_pr_landing_github_effect (OMN-19826).

The wave-2 handler (OMN-19831) sends every request through this protocol. The
live implementation adds the Authorization header from the contract-declared
GITHUB_TOKEN ref; the recorded fake in the unit tests replays fixtures.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)


@runtime_checkable
class ProtocolPrLandingGithubTransport(Protocol):
    """Send one GitHub API request and return the response, headers included."""

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        """Return the response for any HTTP status; raise only when none arrived."""
        ...
