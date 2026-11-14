# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Identity seam of node_pr_landing_github_effect (OMN-23777).

An arm, enqueue, update-branch or disarm is sent through the transport this
protocol returns: one authenticated as the writer App's repository-scoped
installation token. Reads and re-runs keep the contract's ``GITHUB_TOKEN``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.nodes.node_pr_landing_github_effect.models import (
    EnumPrLandingGithubFailureReason,
)
from omnimarket.nodes.node_pr_landing_github_effect.protocols.protocol_pr_landing_github_transport import (
    ProtocolPrLandingGithubTransport,
)


class LandingIdentityRefusedError(RuntimeError):
    """No App-identity transport can be built; nothing was sent.

    ``reason`` is the typed failure the effect reports. The message names refs,
    permission names and HTTP statuses, never a token.
    """

    def __init__(self, reason: EnumPrLandingGithubFailureReason, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@runtime_checkable
class ProtocolLandingMutationTransportSource(Protocol):
    """Build the App-identity transport for one repository's mutation."""

    @property
    def identity(self) -> str:
        """A credential label for the quota reading, never a token value."""

    async def transport_for(self, repository: str) -> ProtocolPrLandingGithubTransport:
        """Return a transport for ``OWNER/NAME`` or raise
        :class:`LandingIdentityRefusedError` before any request is made."""


__all__ = ["LandingIdentityRefusedError", "ProtocolLandingMutationTransportSource"]
