# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the reducer's authority over the ledger, for the fenced flip (OMN-23154)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from omnimarket.models.work_ledger_append import (
    ModelAuthorityDecision,
    ModelFileJudgeRestartState,
    ModelWorkLedgerJudgeAcceptance,
)


class ProtocolLedgerAuthority(Protocol):
    """The reducer: the single writer that judges a command and gives each row its ledger_seq."""

    def submit(
        self, request_id: str, rows: str, *, if_absent_cell: str | None = None
    ) -> ModelAuthorityDecision:
        """Judge one command in one transaction; a request id already decided answers ``duplicate``."""
        ...

    def adopt(self, acceptances: Sequence[ModelWorkLedgerJudgeAcceptance]) -> None:
        """Take the rows the file judge accepted before the flip, so the reducer knows every req=."""
        ...

    def decision_for(self, request_id: str) -> ModelAuthorityDecision | None:
        """The earlier decision of a request id, or None when the reducer holds none."""
        ...

    def highest_seq(self) -> int:
        """The highest committed ledger_seq, 0 when the reducer holds no row."""
        ...

    def row_id_at(self, seq: int) -> str | None:
        """The content hash of the row committed at ``seq``, or None."""
        ...

    def decided_request_ids(self) -> tuple[str, ...]:
        """Every request id the reducer holds a decision for."""
        ...


class ProtocolFileJudgeRestart(Protocol):
    """Starts the old file judge again from the reducer's records, without writing the file."""

    def restart(self, state: ModelFileJudgeRestartState) -> None: ...


__all__: list[str] = ["ProtocolFileJudgeRestart", "ProtocolLedgerAuthority"]
