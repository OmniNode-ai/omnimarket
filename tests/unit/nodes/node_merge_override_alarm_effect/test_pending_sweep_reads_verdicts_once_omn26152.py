# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26152: a sweep over N pending rows costs a fixed number of database reads, not N.

The profiled dev runtime spent 20.5% of its active samples in
``pending_older_than`` -> ``_has_verdict``: one adapter query (one Postgres
connect) per pending row, and pending rows are never deleted.
"""

from __future__ import annotations

from datetime import timedelta

from omnimarket.nodes.node_merge_override_alarm_effect.handlers.store_merge_override import (
    PENDING,
    VERDICTS,
    MergeOverrideStore,
    repo_pr,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from tests.unit.nodes.node_merge_override_alarm_effect.support import NOW, REPO

ROWS = 200


class CountingAdapter(InmemoryDatabaseAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.queries = 0

    def query(
        self,
        table: str,
        filters: dict[str, object] | None = None,
        *,
        order_by: str | None = None,
        descending: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, object]]:
        self.queries += 1
        return super().query(
            table, filters, order_by=order_by, descending=descending, limit=limit
        )


def _seed(db: CountingAdapter) -> None:
    old = NOW - timedelta(hours=2)
    for n in range(ROWS):
        key = repo_pr(REPO, n)
        db.upsert(PENDING, "repo_pr", {"repo_pr": key, "requested_at": old})
        if n % 2 == 0:
            db.upsert(VERDICTS, "repo_pr", {"repo_pr": key})


def test_sweep_cost_does_not_grow_with_pending_rows() -> None:
    db = CountingAdapter()
    _seed(db)
    db.queries = 0
    rows = MergeOverrideStore(db).pending_older_than(NOW - timedelta(minutes=10))
    assert len(rows) == ROWS // 2
    assert db.queries <= 2, f"{db.queries} adapter queries for {ROWS} pending rows"
