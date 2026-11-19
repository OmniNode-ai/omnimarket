# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory adapters: the default profile and the tests' fakes (guard state, episode store, ledger appender)."""

from __future__ import annotations

from typing import Any

from omnimarket.nodes.node_reading_writer_effect.protocols import ModelAppendOutcome

from ..models import EnumGuardReadState, ModelGuardRead


class InMemoryGuardState:
    """The guard's state in memory. ``latch``, ``last`` and ``outbox`` can be set to any read; a claim is kept until settled."""

    def __init__(self) -> None:
        self.latch = ModelGuardRead(EnumGuardReadState.ABSENT)
        self.last = ModelGuardRead(EnumGuardReadState.ABSENT)
        self.outbox: list[str] | None = []
        self.claimed: list[str] | None = None
        self.refused: list[tuple[str, str]] = []
        self.settle_error: OSError | None = None

    def read_latch(self) -> ModelGuardRead:
        return self.latch

    def read_last(self) -> ModelGuardRead:
        return self.last

    def peek_outbox(self) -> list[str] | None:
        if self.outbox is None:
            return None
        return [*(self.claimed or []), *self.outbox]

    def claim_outbox(self) -> list[str] | None:
        if self.outbox is None:
            return None
        if self.claimed is None:
            self.claimed, self.outbox = list(self.outbox), []
        return list(self.claimed)

    def settle_outbox(self, kept: list[str], refused: list[tuple[str, str]]) -> None:
        if self.settle_error is not None:
            raise self.settle_error
        self.refused.extend(refused)
        self.outbox = [*(self.outbox or []), *kept]
        self.claimed = None


class InMemoryEpisodeStore:
    """The episode file in memory; ``record`` None reads as unreadable."""

    def __init__(self) -> None:
        self.record: dict[str, Any] | None = {}
        self.save_error: OSError | None = None

    def load(self) -> dict[str, Any] | None:
        return None if self.record is None else dict(self.record)

    def save(self, episode: dict[str, Any]) -> None:
        if self.save_error is not None:
            raise self.save_error
        self.record = dict(episode)


class InMemoryLedgerAppender:
    """Records every append; ``outcomes`` maps a request id to a status, ``default`` is the status of any other."""

    def __init__(self, default: str = "accepted") -> None:
        self.default = default
        self.outcomes: dict[str, str] = {}
        self.sent: list[tuple[str, str]] = []

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
        self.sent.append((request_id, rows))
        status = self.outcomes.get(request_id, self.default)
        return ModelAppendOutcome(
            status=status,
            message="" if status in ("accepted", "duplicate") else f"injected {status}",
        )
