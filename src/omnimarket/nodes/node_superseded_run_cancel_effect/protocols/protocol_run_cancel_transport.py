# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Transport seam of node_superseded_run_cancel_effect (OMN-17427)."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)


@runtime_checkable
class ProtocolRunCancelTransport(Protocol):
    """Send one GitHub API request and return the response for any HTTP status."""

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        """Return the response; raise only when none arrived."""
        ...
