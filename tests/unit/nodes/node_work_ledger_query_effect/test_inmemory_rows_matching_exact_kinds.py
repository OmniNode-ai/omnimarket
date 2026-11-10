# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory narrowed read keeps row types exactly, as the Postgres reader does (OMN-19609).

The Postgres reader filters with ``row_type = ANY(%s)``, which is case-sensitive, and the
protocol says ``kinds`` keeps rows of those row types exactly. The in-memory reader
upper-cased both sides, so a row of a non-canonical case matched there and not in the
store, and the two readers answered one request differently.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_inmemory import (
    InMemoryWorkLedgerQueryReader,
)

pytestmark = pytest.mark.unit

STAMP = "2026-11-07T10:00:00Z"
SINCE = datetime(2026, 11, 1, tzinfo=UTC)
UNTIL = datetime(2026, 11, 8, tzinfo=UTC)


def _reader() -> InMemoryWorkLedgerQueryReader:
    reader = InMemoryWorkLedgerQueryReader()
    reader.ingest(
        {
            "row_id": "upper",
            "row_type": "STATUS",
            "raw_row": f"{STAMP} | STATUS | lane=a | text",
        }
    )
    reader.ingest(
        {
            "row_id": "lower",
            "row_type": "status",
            "raw_row": f"{STAMP} | status | lane=b | text",
        }
    )
    return reader


def test_canonical_kind_keeps_only_the_exact_row_type() -> None:
    rows = _reader().read_rows_matching(
        since=SINCE, until=UNTIL, kinds=("STATUS",), contains=None
    )
    assert [r.row_id for r in rows] == ["upper"]


def test_lower_case_kind_does_not_match_the_canonical_row_type() -> None:
    rows = _reader().read_rows_matching(
        since=SINCE, until=UNTIL, kinds=("status",), contains=None
    )
    assert [r.row_id for r in rows] == ["lower"]
