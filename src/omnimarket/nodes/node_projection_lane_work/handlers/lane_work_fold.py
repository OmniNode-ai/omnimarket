# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure fold of lane work receipts into workflow rows (OMN-26708).

A row is the fold of its workflow's receipts. `row_from_receipt` is the row one
receipt describes on its own; `merge_receipt` joins the next receipt of the same
workflow onto it. A receipt whose `seq` is not past the row's is refused, which
is what makes a replay of the transition topic a no-op: the writer's guarded SQL
(`WHERE seq < EXCLUDED.seq`) takes its parameters from `row_from_receipt` and
applies the same rule, so the two cannot disagree.
"""

from __future__ import annotations

from collections.abc import Iterable

from omnibase_core.enums.enum_lane_work_state import EnumLaneWorkState
from omnibase_core.models.lane_work import ModelLaneWorkReceipt

from omnimarket.nodes.node_projection_lane_work.models.model_lane_work_row import (
    ModelLaneWorkRow,
)

TERMINAL_STATES: frozenset[EnumLaneWorkState] = frozenset(
    {EnumLaneWorkState.HANDED_OFF, EnumLaneWorkState.BLOCKED}
)


def _is_transition(receipt: ModelLaneWorkReceipt) -> bool:
    return receipt.kind == "transition"


def _enters_help(receipt: ModelLaneWorkReceipt) -> bool:
    return (
        _is_transition(receipt)
        and receipt.to_state is EnumLaneWorkState.HELP_REQUESTED
        and receipt.from_state is not EnumLaneWorkState.HELP_REQUESTED
    )


def _ends(receipt: ModelLaneWorkReceipt) -> bool:
    return _is_transition(receipt) and receipt.to_state in TERMINAL_STATES


def row_from_receipt(receipt: ModelLaneWorkReceipt) -> ModelLaneWorkRow:
    """The row this one receipt describes: the contribution the writer upserts."""
    return ModelLaneWorkRow(
        workflow_id=receipt.workflow_id,
        state=receipt.to_state,
        last_event_kind=receipt.event_kind.value,
        seq=receipt.seq,
        started_at=receipt.occurred_at,
        last_receipt_at=receipt.occurred_at,
        transitions_taken=1 if _is_transition(receipt) else 0,
        help_request_count=1 if _enters_help(receipt) else 0,
        duration_seconds=0.0,
        terminal_kind=receipt.to_state.value if _ends(receipt) else None,
        ended_at=receipt.occurred_at if _ends(receipt) else None,
    )


def merge_receipt(
    row: ModelLaneWorkRow | None, receipt: ModelLaneWorkReceipt
) -> ModelLaneWorkRow:
    """Join one receipt onto its workflow's row; a receipt not past `seq` changes nothing."""
    contribution = row_from_receipt(receipt)
    if row is None:
        return contribution
    if receipt.seq <= row.seq:
        return row
    started_at = min(row.started_at, contribution.started_at)
    return contribution.model_copy(
        update={
            "started_at": started_at,
            "transitions_taken": row.transitions_taken + contribution.transitions_taken,
            "help_request_count": row.help_request_count
            + contribution.help_request_count,
            "duration_seconds": (
                contribution.last_receipt_at - started_at
            ).total_seconds(),
            "terminal_kind": contribution.terminal_kind or row.terminal_kind,
            "ended_at": contribution.ended_at or row.ended_at,
        }
    )


def fold_receipts(
    receipts: Iterable[ModelLaneWorkReceipt],
) -> dict[str, ModelLaneWorkRow]:
    """One row per workflow, folded in the order the receipts arrive."""
    rows: dict[str, ModelLaneWorkRow] = {}
    for receipt in receipts:
        rows[receipt.workflow_id] = merge_receipt(
            rows.get(receipt.workflow_id), receipt
        )
    return rows


def terminal_rows(rows: dict[str, ModelLaneWorkRow]) -> list[ModelLaneWorkRow]:
    """The orchestrator's view: only the workflows that ended."""
    return sorted(
        (row for row in rows.values() if row.terminal_kind is not None),
        key=lambda row: row.workflow_id,
    )
