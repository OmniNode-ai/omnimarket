# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Measure missing transitions with the projection's canonical parity fold."""

from datetime import datetime

from omnimarket.nodes.node_branch_claim_check_effect.handlers.branch_claim_resolution import (
    _row_class,
    _subjects,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.work_ledger_fold import (
    parse_stamp,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_parity_report import (
    EnumParityMismatchKind,
)
from omnimarket.nodes.node_projection_work_ledger.parity import compare


def missing_claim_rows(
    *,
    witness_rows: list[str],
    db_row_ids: frozenset[str],
    ticket: str | None,
    since: datetime,
    until: datetime,
    transition_rows: tuple[str, ...],
) -> list[str]:
    """None selects all tickets for visibility; a ticket selects its gate."""
    selected = [
        row
        for row in witness_rows
        if _row_class(row) in transition_rows
        and (ticket is None or ticket in _subjects(row))
        and since <= parse_stamp(row.split("|", 1)[0].strip()) <= until
    ]
    report = compare(
        file_rows=selected,
        projection_rows=dict.fromkeys(db_row_ids, ""),
        projection_state={},
        since=since,
        until=until,
    )
    return [
        mismatch.detail
        for mismatch in report.mismatches
        if mismatch.kind is EnumParityMismatchKind.ROW_MISSING_IN_PROJECTION
    ]
