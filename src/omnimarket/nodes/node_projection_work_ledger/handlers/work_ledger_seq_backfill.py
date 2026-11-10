# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The history backfill's plan: give rows with no ledger_seq a sequence in file order (OMN-23152).

Pure. Input: the row ids in the order the file holds them and the sequences some of them already
carry. Output: the assignments that make the sequence strictly increase along the file with no gap
and no reuse. A held sequence is never moved. A history whose held sequences already disagree with
file order, or whose gaps do not fit the rows missing between them, is refused: the plan never
guesses an order.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


class SeqBackfillRefusedError(ValueError):
    """The held sequences and the file order cannot be reconciled without a guess."""


def plan_seq_backfill(
    file_order: Sequence[str],
    held: Mapping[str, int],
    *,
    first_seq: int = 1,
) -> dict[str, int]:
    """The sequence for each row of ``file_order`` that holds none, keeping file order."""
    if len(set(file_order)) != len(file_order):
        raise SeqBackfillRefusedError("a row id appears twice in the file order")
    unknown = sorted(set(held) - set(file_order))
    if unknown:
        raise SeqBackfillRefusedError(
            f"{len(unknown)} held row id(s) are not in the file order, first {unknown[0][:12]}"
        )
    plan: dict[str, int] = {}
    previous_seq = first_seq - 1
    pending: list[str] = []
    for row_id in file_order:
        seq = held.get(row_id)
        if seq is None:
            pending.append(row_id)
            continue
        if seq <= previous_seq:
            raise SeqBackfillRefusedError(
                f"row {row_id[:12]} holds {seq} after sequence {previous_seq} in file order"
            )
        if len(pending) != seq - previous_seq - 1:
            raise SeqBackfillRefusedError(
                f"{len(pending)} row(s) with no sequence lie before {seq} "
                f"and the gap after {previous_seq} holds {seq - previous_seq - 1}"
            )
        for offset, pending_id in enumerate(pending, start=1):
            plan[pending_id] = previous_seq + offset
        pending = []
        previous_seq = seq
    for offset, pending_id in enumerate(pending, start=1):
        plan[pending_id] = previous_seq + offset
    return plan


__all__: list[str] = ["SeqBackfillRefusedError", "plan_seq_backfill"]
