# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""query_default_window: a rows read is bounded by count by default; a full read needs a flag.

The ledger's query verb wrote the whole ledger (100,000 to 190,000 lines) into one capture file
per call. The time window of a no-since read already existed; a window can still hold more rows
than a capture should carry, so the default also caps the count. The count comes from the node's
overlay (``default_rows_limit``) or the contract (``work_ledger_query.rows_default_limit``), never
from code, and ``full=True`` is the explicit flag that reads everything. The 150,000-row ledger is
generated here and never committed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
import yaml

from omnimarket.models.work_ledger_query import (
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerQueryResult,
    ModelWorkLedgerRowRecord,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.handler_work_ledger_query import (
    HandlerWorkLedgerQuery,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    ProtocolWorkLedgerQueryReader,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).parents[4]
    / "src/omnimarket/nodes/node_work_ledger_query_effect/contract.yaml"
)
UNTIL = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
ROWS = 150_000


class Store:
    """A reader over a generated ledger; every row sits inside the default time window."""

    def __init__(self, records: tuple[ModelWorkLedgerRowRecord, ...]) -> None:
        self.records = records

    def read_rows(
        self, *, since: datetime | None, until: datetime
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        return tuple(
            r
            for r in self.records
            if (since is None or r.row_ts >= since) and r.row_ts <= until
        )

    def read_parity_receipts(
        self, *, since: datetime, until: datetime
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        return ()

    def freshness(self, *, until: datetime) -> tuple[datetime | None, datetime | None]:
        return self.records[-1].row_ts, self.records[-1].row_ts


def _ledger(count: int) -> tuple[ModelWorkLedgerRowRecord, ...]:
    start = UNTIL - timedelta(hours=20)
    out: list[ModelWorkLedgerRowRecord] = []
    for i in range(count):
        stamp = start + timedelta(milliseconds=i * 400)
        text = (
            f"{stamp.strftime('%Y-%m-%dT%H:%M:%SZ')} | STATUS | lane=l{i % 7} | n={i}"
        )
        out.append(
            ModelWorkLedgerRowRecord(
                row_id=f"r{i}",
                row_ts=stamp,
                row_type="STATUS",
                row_lane=f"l{i % 7}",
                text=text,
                raw_row=text,
                source="onex-ledger",
                projected_at=stamp,
            )
        )
    return tuple(out)


def _contract_limit() -> int:
    with CONTRACT.open(encoding="utf-8") as handle:
        limit = yaml.safe_load(handle)["work_ledger_query"]["rows_default_limit"]
    assert isinstance(limit, int)
    return limit


def _ask(store: Store, contract: Path, **kwargs: object) -> ModelWorkLedgerQueryResult:
    request = ModelWorkLedgerQueryRequest(
        correlation_id=uuid4(), query="rows", now=UNTIL, until=UNTIL, **kwargs
    )
    reader = cast(ProtocolWorkLedgerQueryReader, store)
    handler = HandlerWorkLedgerQuery(
        contract_path=contract, reader=reader, now=lambda: UNTIL
    )
    # A full read is a local read, not one record on the bus: lift the bus answer bound.
    handler._answer_bound = handler._answer_bound.model_copy(
        update={"max_answer_bytes": 2**40}
    )
    return handler.handle(request)


@pytest.fixture(scope="module")
def big_store() -> Store:
    return Store(_ledger(ROWS))


def test_query_default_window_caps_the_count_and_full_reads_every_row(
    big_store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_LEDGER_QUERY_OVERLAY", raising=False)
    contract = CONTRACT
    limit = _contract_limit()
    assert limit < ROWS
    bounded = _ask(big_store, contract)
    assert bounded.error is None
    assert len(bounded.rows) <= limit
    assert bounded.matched == ROWS
    assert bounded.truncated is True
    assert bounded.bounded_rows_limit == limit
    assert (
        bounded.rows[-1].row_id == f"r{ROWS - 1}"
    )  # the newest rows are the ones kept
    full = _ask(big_store, contract, full=True)
    assert full.error is None
    assert len(full.rows) == ROWS
    assert full.truncated is False
    assert full.bounded is False


def test_query_default_window_overlay_count_wins_over_the_contract(
    big_store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(
        "default_rows_window_days: 3\ndefault_rows_limit: 17\n", encoding="utf-8"
    )
    monkeypatch.setenv("ONEX_LEDGER_QUERY_OVERLAY", str(overlay))
    result = _ask(big_store, CONTRACT)
    assert len(result.rows) == 17
    assert result.bounded_rows_limit == 17


def test_query_default_window_an_explicit_limit_is_not_replaced(
    big_store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ONEX_LEDGER_QUERY_OVERLAY", raising=False)
    result = _ask(big_store, CONTRACT, limit=_contract_limit() + 5)
    assert len(result.rows) == _contract_limit() + 5
    assert result.bounded_rows_limit is None
