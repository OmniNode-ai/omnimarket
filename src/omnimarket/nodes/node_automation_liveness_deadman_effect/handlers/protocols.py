# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two ends the dead-man's recording plugs into (OMN-20807)."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_automation_liveness_deadman_compute.models.model_automation_liveness_deadman import (
    ModelDeadmanEpisodeKey,
)


class ProtocolLedgerAppender(Protocol):
    """Append one row to the ledger.

    ``find_line`` is the ledger read before any append: a row already there is
    recorded as written instead of appended again, so a crash between an append
    and the store costs no duplicate. ``append`` returns the ledger line it
    wrote and raises when the ledger refuses or cannot be reached.
    """

    def find_line(self, key: ModelDeadmanEpisodeKey) -> str | None: ...

    def append(self, key: ModelDeadmanEpisodeKey, row: str) -> str: ...


class UnwiredLedgerAppender:
    """Stands in until a ledger is wired; every write fails loudly."""

    def find_line(self, key: ModelDeadmanEpisodeKey) -> str | None:
        return None

    def append(self, key: ModelDeadmanEpisodeKey, row: str) -> str:
        raise RuntimeError("no ledger appender is wired")
