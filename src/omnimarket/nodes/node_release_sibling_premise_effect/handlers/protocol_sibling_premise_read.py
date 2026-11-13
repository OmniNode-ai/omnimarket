# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The outside reads of the release sibling-premise read effect, each behind a protocol."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol
from uuid import UUID

from omnimarket.nodes.node_dev_merge_redeploy_trigger_effect.models.model_dev_merge_redeploy import (
    ModelDevMergeRedeployReceipt,
)


class ProtocolReceiptSource(Protocol):
    async def read(
        self, topic: str, since: datetime
    ) -> tuple[ModelDevMergeRedeployReceipt, ...]:
        """The receipts on ``topic`` at or after ``since``, oldest first."""
        ...

    def skipped(self) -> int:
        """Records on the topic that were not a receipt."""
        ...


class ProtocolBranchHistory(Protocol):
    def head(self, clone_dir: str, branch: str) -> str:
        """The 40-hex commit at the tip of ``branch`` in the clone."""
        ...

    def first_parent_walk(
        self, clone_dir: str, branch: str, limit: int
    ) -> tuple[str, ...]:
        """Up to ``limit`` first-parent commits back from the head, newest first."""
        ...

    def commit_time(self, clone_dir: str, sha: str) -> datetime:
        """The committer time of ``sha`` (aware)."""
        ...

    def descendants(self, clone_dir: str, branch: str, sha: str) -> tuple[str, ...]:
        """The first-parent commits after ``sha`` up to the head, 40-hex each."""
        ...


class ProtocolVerdictSink(Protocol):
    async def publish(
        self, topic: str, payload: dict[str, object], correlation_id: UUID
    ) -> None:
        """Publish ``payload`` on ``topic`` with ``correlation_id``."""
        ...
