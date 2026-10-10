# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_github_repo_write_effect (OMN-20912).

The GitHub transport is the shared landing transport's ``send`` (the live
``UrllibGithubLandingTransport`` satisfies it); tests inject a recorded fake.
The claim lookup answers whether a lane holds a live claim on a PR in
node_pr_claim_registry_effect; the live lookup calls that registry's own
handler.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.github_landing.model_github_http_exchange import (
    ModelGithubHttpRequest,
    ModelGithubHttpResponse,
)
from omnimarket.models.pr_claim import ModelPrClaim


@runtime_checkable
class ProtocolGithubRepoWriteTransport(Protocol):
    """Send one typed GitHub request; any HTTP status comes back whole."""

    async def send(self, request: ModelGithubHttpRequest) -> ModelGithubHttpResponse:
        """Raises ``GithubLandingTransportError`` only when no response arrived."""
        ...


@runtime_checkable
class ProtocolPrClaimLookup(Protocol):
    """Read the claim on one PR from the claim registry."""

    def active_claim(
        self, *, claims_dir: str, pr_key: str, now: str
    ) -> ModelPrClaim | None:
        """The PR's claim when it is live at ``now``; None when absent or expired."""
        ...


__all__: list[str] = ["ProtocolGithubRepoWriteTransport", "ProtocolPrClaimLookup"]
