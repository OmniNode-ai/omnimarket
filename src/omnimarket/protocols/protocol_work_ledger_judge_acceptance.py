# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports for the judge's acceptance record (OMN-23152)."""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.work_ledger_append.model_work_ledger_judge_acceptance import (
    ModelWorkLedgerJudgeAcceptance,
)


class ProtocolJudgeAcceptanceRecorder(Protocol):
    """Written by the judge when it accepts a row."""

    def record(self, acceptance: ModelWorkLedgerJudgeAcceptance) -> None: ...


class ProtocolJudgeAcceptanceReader(Protocol):
    """Read by the emit gate: the judge's acceptance of a row, or None when it holds none."""

    def acceptance_for(self, row_id: str) -> ModelWorkLedgerJudgeAcceptance | None: ...


class ProtocolJudgeSequenceSource(Protocol):
    """The judge's own sequence for a row it just appended, or None when it assigned none."""

    def sequence_of(self, row_id: str) -> int | None: ...


__all__: list[str] = [
    "ProtocolJudgeAcceptanceReader",
    "ProtocolJudgeAcceptanceRecorder",
    "ProtocolJudgeSequenceSource",
]
