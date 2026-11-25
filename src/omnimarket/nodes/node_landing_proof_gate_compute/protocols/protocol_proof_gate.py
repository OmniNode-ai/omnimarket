# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three readers the landing proof gate takes. The handler imports no reader: each one comes in here."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_landing_proof_gate_compute.models.model_proof_gate import (
    ModelLedgerNotice,
    ModelProofText,
)


class ProtocolProofReader(Protocol):
    def read(
        self, repo: str, number: int, base: str, head: str
    ) -> ModelProofText | None:
        """Body, comments and changed files of the pull request at ``head``, or None when unreadable."""
        ...


class ProtocolLedgerReader(Protocol):
    def open_notices(
        self, repo: str, number: int
    ) -> tuple[ModelLedgerNotice, ...] | None:
        """The HOLD rows and do-not-land messages naming the pull request that no RELEASE lifted, or None."""
        ...


class ProtocolPatchIdReader(Protocol):
    def patch_id(self, repo: str, base: str, sha: str) -> str | None:
        """The patch id of ``base...sha``, or None when the commit or the base cannot be read."""
        ...
