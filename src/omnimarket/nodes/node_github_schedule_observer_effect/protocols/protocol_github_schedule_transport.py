# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Transport seam of node_github_schedule_observer_effect (OMN-20803).

The live implementation is the shared landing transport
(``omnimarket.github_landing``), which adds the Authorization header from the
contract-declared GITHUB_TOKEN ref; the recorded fake in the tests replays
fixtures.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)


@runtime_checkable
class ProtocolGithubScheduleTransport(Protocol):
    """Send one GitHub API request and return the response, headers included."""

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        """Return the response for any HTTP status; raise only when none arrived."""
        ...
