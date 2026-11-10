# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory judge acceptance store and the emit gate's pure decision (OMN-23152).

The in-memory store is the default backend for both ports: the judge writes it, the emit gate
reads it. A real-backend overlay binds the same ports to the durable acceptance record.
The decision is pure: it takes the gate settings, the row's content hash, the sequence the
caller states and the reader, and returns the refusal text or None.
"""

from __future__ import annotations

from omnimarket.models.work_ledger_append.model_work_ledger_judge_acceptance import (
    ModelWorkLedgerJudgeAcceptance,
    ModelWorkLedgerJudgeGate,
)
from omnimarket.protocols.protocol_work_ledger_judge_acceptance import (
    ProtocolJudgeAcceptanceReader,
)


class InMemoryJudgeAcceptanceStore:
    """Holds the acceptances of one process; implements recorder and reader."""

    def __init__(self) -> None:
        self._by_row_id: dict[str, ModelWorkLedgerJudgeAcceptance] = {}

    def record(self, acceptance: ModelWorkLedgerJudgeAcceptance) -> None:
        # A stored acceptance is immutable: the first sequence the judge gave a row stands.
        self._by_row_id.setdefault(acceptance.row_id, acceptance)

    def acceptance_for(self, row_id: str) -> ModelWorkLedgerJudgeAcceptance | None:
        return self._by_row_id.get(row_id)


def judge_gate_refusal(
    gate: ModelWorkLedgerJudgeGate,
    *,
    row_id: str,
    stated_seq: int | None,
    reader: ProtocolJudgeAcceptanceReader | None,
) -> str | None:
    """The refusal for a row the judge did not accept, else None. Pure."""
    if not gate.require_acceptance:
        return None
    if stated_seq is None:
        return (
            f"{gate.refusal_prefix}: row {row_id[:12]} carries no ledger_seq; "
            "only a row the judge accepted reaches the bus"
        )
    if reader is None:
        return (
            f"{gate.refusal_prefix}: no judge acceptance record is bound, "
            f"so row {row_id[:12]} cannot be shown accepted"
        )
    held = reader.acceptance_for(row_id)
    if held is None:
        return (
            f"{gate.refusal_prefix}: the judge holds no acceptance of row {row_id[:12]}"
        )
    if held.ledger_seq != stated_seq:
        return (
            f"{gate.refusal_prefix}: row {row_id[:12]} states ledger_seq {stated_seq} "
            f"and the judge assigned {held.ledger_seq}"
        )
    return None


__all__: list[str] = ["InMemoryJudgeAcceptanceStore", "judge_gate_refusal"]
