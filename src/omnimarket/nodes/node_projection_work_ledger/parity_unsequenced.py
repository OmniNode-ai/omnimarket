# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The unsequenced extra rows of the work-ledger parity explain (OMN-21849).

A projection row with a NULL ``ledger_seq`` is the mark of a dual-write emit from a ledger file that is not the
canonical one: the canonical append assigns every row a sequence. The parity ``--explain`` and ``--receipt`` read
the window's NULL-sequence row ids (from a projection export or the database) and split the ``extra`` rows by them.
An export entry with no ``ledger_seq`` key is unknown, never unsequenced. The per-source row count the
backfilled flag reads sits beside it (moved out of the baselined ``parity.py``, behaviour unchanged).
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

from omnimarket.handlers.work_ledger_fold import parse_stamp
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_parity_report import (
    EnumParityMismatchKind,
    ModelParityExplain,
    ModelWorkLedgerParityReport,
)

SELECT_SOURCES = """
    SELECT source, count(*) AS n FROM omninode_internal.work_ledger_rows
    WHERE row_ts >= $1 AND row_ts <= $2 GROUP BY source
"""
SELECT_UNSEQUENCED = """
    SELECT row_id FROM omninode_internal.work_ledger_rows
    WHERE row_ts >= $1 AND row_ts <= $2 AND ledger_seq IS NULL
"""


def load_projection_unsequenced_json(
    path: Path, since: datetime, until: datetime
) -> frozenset[str]:
    """Row ids in the window whose export HAS a ``ledger_seq`` key holding None."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return frozenset(
        str(r["row_id"])
        for r in data.get("rows", [])
        if "ledger_seq" in r
        and r["ledger_seq"] is None
        and since <= parse_stamp(str(r["row_ts"])) <= until
    )


async def load_unsequenced_db(
    dsn: str, since: datetime, until: datetime
) -> frozenset[str]:
    """Row ids in the window whose projection ``ledger_seq`` is NULL, read from the database."""
    import asyncpg  # lazy: only the live read needs a database driver

    conn = await asyncpg.connect(dsn)
    try:
        return frozenset(
            str(r["row_id"]) for r in await conn.fetch(SELECT_UNSEQUENCED, since, until)
        )
    finally:
        await conn.close()


def read_unsequenced(
    projection_json: Path | None, dsn_env: str, since: datetime, until: datetime
) -> frozenset[str]:
    """The window's NULL-sequence row ids from the export when one is given, else from the database named by the ``dsn_env`` variable."""
    if projection_json is not None:
        return load_projection_unsequenced_json(projection_json, since, until)
    return asyncio.run(load_unsequenced_db(os.environ[dsn_env], since, until))


def split_extra_rows(
    report: ModelWorkLedgerParityReport,
    unsequenced: frozenset[str],
    explain: ModelParityExplain,
) -> ModelParityExplain:
    """``explain`` with the report's extra rows (in the projection, not in the file) split by ``unsequenced``."""
    extras = {
        m.key
        for m in report.mismatches
        if m.kind is EnumParityMismatchKind.ROW_MISSING_IN_FILE
    }
    return explain.model_copy(
        update={
            "extra_unsequenced": len(extras & unsequenced),
            "extra_sequenced": len(extras - unsequenced),
        }
    )


def load_projection_sources_json(
    path: Path, since: datetime, until: datetime
) -> dict[str, int]:
    """Projected rows in the window per ``source`` (absent counts as ``unknown``)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    counts: Counter[str] = Counter()
    for r in data.get("rows", []):
        if since <= parse_stamp(str(r["row_ts"])[:20]) <= until:
            counts[str(r.get("source") or "unknown")] += 1
    return dict(counts)


async def load_sources_db(dsn: str, since: datetime, until: datetime) -> dict[str, int]:
    import asyncpg  # lazy: only the live read needs a database driver

    conn = await asyncpg.connect(dsn)
    try:
        return {
            str(r["source"]): int(r["n"])
            for r in await conn.fetch(SELECT_SOURCES, since, until)
        }
    finally:
        await conn.close()
