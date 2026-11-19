# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_host_thermal_hold_report_effect: the guard's state, the node's episode file and the ledger appender."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from omnimarket.nodes.node_reading_writer_effect.protocols import ModelAppendOutcome

from ..models import ModelGuardRead


@runtime_checkable
class ProtocolGuardState(Protocol):
    """The guard's state directory. The outbox is claimed (moved aside) before rows are delivered and settled after,
    so a row the guard appends meanwhile lands in a new outbox file and is never overwritten."""

    def read_latch(self) -> ModelGuardRead: ...

    def read_last(self) -> ModelGuardRead: ...

    def peek_outbox(self) -> list[str] | None:
        """The queued rows without claiming them; None when the outbox cannot be read."""
        ...

    def claim_outbox(self) -> list[str] | None:
        """Move the outbox aside and return its rows (a claim a crashed run left is returned first); None when unreadable."""
        ...

    def settle_outbox(self, kept: list[str], refused: list[tuple[str, str]]) -> None:
        """Put ``kept`` rows back into the outbox, append each refused row with its reason to the refused file, release the claim."""
        ...


@runtime_checkable
class ProtocolEpisodeStore(Protocol):
    """The node's own episode file: the alarm is deduplicated by episode."""

    def load(self) -> dict[str, Any] | None:
        """The episode record; {} when there is none yet; None when the file cannot be read."""
        ...

    def save(self, episode: dict[str, Any]) -> None: ...


@runtime_checkable
class ProtocolLedgerAppender(Protocol):
    """Append one ledger row through the declared bus append command (the signature CommandLedgerAppender has)."""

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome: ...


__all__ = ["ProtocolEpisodeStore", "ProtocolGuardState", "ProtocolLedgerAppender"]
