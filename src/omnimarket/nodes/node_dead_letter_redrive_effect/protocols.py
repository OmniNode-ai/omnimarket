# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three boundaries of the dead-letter redrive: backlog, source and publish."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_dead_letter_redrive_effect.models import (
    ModelDlqBacklogEntry,
    ModelSourceRecord,
)


class ProtocolDlqBacklog(Protocol):
    """Read-only view of the dead-letter backlog."""

    def read_backlog(
        self, *, source_topic: str | None, limit: int
    ) -> list[ModelDlqBacklogEntry]:
        """Up to ``limit`` entries, oldest first, optionally from one source topic."""


class ProtocolSourceRecordReader(Protocol):
    """Group-less, commit-free read of one record by topic, partition, offset."""

    async def read_record(
        self, *, topic: str, partition: int, offset: int
    ) -> ModelSourceRecord | None:
        """The record, or None when retention removed it."""


class ProtocolRecordPublisher(Protocol):
    """Publishes one record back to its source topic."""

    async def publish(self, *, topic: str, key: bytes | None, value: bytes) -> None:
        """Publish ``value`` under ``key`` to ``topic``."""


class ProtocolRedriveLog(Protocol):
    """Which dead-letter entries were already redriven, so a rerun does not repeat."""

    def has_redriven(self, entry_key: str) -> bool:
        """True when ``entry_key`` was already redriven."""

    def record_redriven(self, entry_key: str) -> None:
        """Record ``entry_key`` as redriven."""
