# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which claim-transition rows the database lacks, by the ledger's own row identity.

A witness row is missing when its canonical identity,
``model_ledger_row_event.work_ledger_row_id`` (the same content hash the emit
path stamps and the parity check compares), is not among the database's row ids
for the window. A transition row the emit path cannot carry at all is missing
by the same test, and that is deliberate: the database replay cannot see it, so
an answer computed without it is not an answer.
"""

from datetime import datetime

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.handlers.work_ledger_text import ledger_row_stamp
from omnimarket.nodes.node_branch_claim_check_effect.handlers.branch_claim_resolution import (
    _row_class,
    _subjects,
)


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
    return [
        row
        for row in witness_rows
        if _row_class(row) in transition_rows
        and (ticket is None or ticket in _subjects(row))
        and since <= ledger_row_stamp(row) <= until
        and work_ledger_row_id(row) not in db_row_ids
    ]
