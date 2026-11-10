# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A local store serves per-day delegation totals and every run (OMN-21970).

The view, its exposure and the walk cursor belong to node_projection_delegation.

The Overview's Runs per day and Tokens per day read the newest 500 rows of
``onex.snapshot.projection.delegation.decisions.v1``, so with more than 500
runs every older day showed no runs and "not recorded" tokens. The fix serves
``onex.snapshot.projection.delegation.daily.v1``, one row per tenant whose
``days`` list holds every UTC day over ``delegation_events``, and lets a reader
walk every decisions row.

Each test names the failure it exists to catch:

* the per-day exposure is missing, so the read answers 503 (AC1);
* the per-day totals stop at a row cap, so 600 runs over 20 days do not sum to
  600, or a day is missing, repeated or out of order (AC1);
* a run lands on the wrong day: the dashboard places a run on the UTC day of
  ``written_at``, so the view must too;
* the passed, failed and not-recorded counts disagree with the dashboard's
  verdict: a run whose terminal was not ok failed whatever its quality gate
  said, and a run with neither is not recorded, never failed;
* another tenant's runs are counted or served;
* the decisions read loses its newest-first first page, which every current
  reader depends on (AC3);
* the decisions walk cannot reach the runs older than the newest 500 (AC3);
* a store created before the walk cursor never numbers its existing runs.

Every store is a SQLite file under pytest's ``tmp_path``. Reads go through the
real read node over :class:`SqliteTableRowSource`, as ``onex dashboard`` does.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.model_projection_read import ModelProjectionReadRequest
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

_DAILY = "onex.snapshot.projection.delegation.daily.v1"
_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"
_TENANT = "0af28cd0-0654-42e0-a050-358ee8240940"
_OTHER = "11111111-2222-4333-8444-555555555555"
_FIRST_DAY = datetime(2026, 10, 1, tzinfo=UTC)

# Built once: discovering every contract's exposures takes seconds.
_TOPICS = build_projection_topic_map()


def _run(correlation_id: str, at: datetime, **fields: Any) -> dict[str, Any]:
    stamp = at.isoformat()
    row: dict[str, Any] = {
        "correlation_id": correlation_id,
        "tenant_id": _TENANT,
        "task_type": "reasoning",
        "delegated_to": "qwen3.8-27b",
        "model_name": "qwen3.8-27b",
        "quality_gate_passed": True,
        "terminal_ok": True,
        "tokens_input": 100,
        "tokens_output": 10,
        "data_source": "real",
        "created_at": stamp,
        "written_at": stamp,
    }
    row.update(fields)
    return row


def _store(tmp_path: Path, rows: list[dict[str, Any]]) -> Path:
    path = tmp_path / "delegation.sqlite"
    adapter = SqliteDatabaseAdapter(path)
    for row in rows:
        adapter.upsert("delegation_events", "correlation_id", row)
    return path


def _six_hundred_runs_over_twenty_days() -> list[dict[str, Any]]:
    """30 runs on each of 20 UTC days, every one at 23:30 so a local-time day
    boundary would move it."""
    rows = []
    for day in range(20):
        at = _FIRST_DAY + timedelta(days=day, hours=23, minutes=30)
        for n in range(30):
            rows.append(_run(f"r{day:02d}-{n:02d}", at + timedelta(seconds=n)))
    return rows


def _read(
    path: Path,
    topic: str,
    *,
    tenant: str = _TENANT,
    since: str | None = None,
    limit: int | None = None,
) -> tuple[int, dict[str, Any]]:
    handler = HandlerProjectionRead(
        topic_map=_TOPICS, row_source=SqliteTableRowSource(path)
    )
    result = asyncio.run(
        handler.handle(
            ModelProjectionReadRequest(
                topic=topic, tenant_id=tenant, since=since, limit=limit
            )
        )
    )
    return result.http_status, dict(result.response or {})


def _walk(
    path: Path, topic: str, *, limit: int | None = None, tenant: str = _TENANT
) -> list[dict[str, Any]]:
    """Every row of a cursor walk, starting at the walk's origin."""
    status, body = _read(path, topic, limit=limit, tenant=tenant)
    assert status == 200, body
    rows = list(body["rows"])
    cursor = body.get("next_cursor")
    for _ in range(100):
        if cursor is None:
            return rows
        status, body = _read(path, topic, since=cursor, limit=limit, tenant=tenant)
        assert status == 200, body
        rows.extend(body["rows"])
        cursor = body.get("next_cursor")
    raise AssertionError("the walk did not end within 100 pages")


def _days(path: Path, tenant: str = _TENANT) -> list[dict[str, Any]]:
    """The one per-day row of a tenant: its ``days`` list, oldest first."""
    status, body = _read(path, _DAILY, tenant=tenant)
    assert status == 200, body
    assert body["row_count"] == 1, body
    assert body["rows"][0]["tenant_id"] == tenant
    days = body["rows"][0]["days"]
    assert isinstance(days, list), days
    return days


def test_the_per_day_exposure_is_served(tmp_path: Path) -> None:
    days = _days(_store(tmp_path, [_run("one", _FIRST_DAY)]))
    assert [(day["day"], day["runs"]) for day in days] == [("2026-10-01", 1)]


def test_six_hundred_runs_over_twenty_days_sum_to_six_hundred(
    tmp_path: Path,
) -> None:
    path = _store(tmp_path, _six_hundred_runs_over_twenty_days())
    days = _days(path)
    assert [day["day"] for day in days] == [
        (_FIRST_DAY + timedelta(days=d)).date().isoformat() for d in range(20)
    ]
    assert all(day["runs"] == 30 for day in days)
    assert sum(day["runs"] for day in days) == 600
    assert sum(day["tokensIn"] for day in days) == 60_000
    assert sum(day["tokensOut"] for day in days) == 6_000
    assert _read(path, _DAILY)[1]["rows"][0]["total_runs"] == 600


def test_a_run_is_placed_on_the_utc_day_of_written_at(tmp_path: Path) -> None:
    created = datetime(2026, 10, 1, 23, 59, tzinfo=UTC)
    written = datetime(2026, 10, 2, 0, 1, tzinfo=UTC)
    path = _store(
        tmp_path,
        [_run("late", created, written_at=written.isoformat())],
    )
    assert [(day["day"], day["runs"]) for day in _days(path)] == [("2026-10-02", 1)]


def test_the_verdict_counts_match_the_dashboard(tmp_path: Path) -> None:
    at = _FIRST_DAY
    path = _store(
        tmp_path,
        [
            _run("passed", at),
            _run("gate-failed", at, quality_gate_passed=False),
            _run("terminal-failed", at, terminal_ok=False, quality_gate_passed=True),
            _run("no-verdict", at, terminal_ok=None, quality_gate_passed=None),
        ],
    )
    day = _days(path)[0]
    assert (day["runs"], day["passed"], day["failed"], day["unrecorded"]) == (
        4,
        1,
        2,
        1,
    )


def test_another_tenants_runs_are_neither_counted_nor_served(
    tmp_path: Path,
) -> None:
    at = _FIRST_DAY
    path = _store(
        tmp_path,
        [
            _run("mine", at),
            _run("theirs-1", at, tenant_id=_OTHER),
            _run("theirs-2", at, tenant_id=_OTHER),
        ],
    )
    assert [day["runs"] for day in _days(path)] == [1]
    assert [day["runs"] for day in _days(path, _OTHER)] == [2]


def test_the_decisions_first_page_is_still_the_newest_runs(tmp_path: Path) -> None:
    path = _store(tmp_path, _six_hundred_runs_over_twenty_days())
    status, body = _read(path, _DECISIONS)
    assert status == 200, body
    assert body["row_count"] == 500
    newest = max(row["written_at"] for row in body["rows"])
    oldest_served = min(row["written_at"] for row in body["rows"])
    assert newest.startswith("2026-10-20")
    assert oldest_served > "2026-10-04"


def test_the_decisions_walk_reaches_every_run(tmp_path: Path) -> None:
    path = _store(tmp_path, _six_hundred_runs_over_twenty_days())
    ids = [row["correlation_id"] for row in _walk(path, _DECISIONS)]
    assert len(set(ids)) == 600


def test_a_store_created_before_the_cursor_numbers_its_runs_on_next_open(
    tmp_path: Path,
) -> None:
    path = _store(tmp_path, _six_hundred_runs_over_twenty_days())
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER delegation_events_cursor_on_insert")
    conn.execute("DROP INDEX idx_delegation_events_projection_cursor")
    conn.execute("ALTER TABLE delegation_events DROP COLUMN projection_cursor")
    conn.execute(
        "DELETE FROM omnimarket_sqlite_store_steps "
        "WHERE step = 'omn21970_delegation_events_cursor'"
    )
    conn.commit()
    conn.close()

    SqliteDatabaseAdapter(path)._connect().close()

    ids = [row["correlation_id"] for row in _walk(path, _DECISIONS)]
    assert len(set(ids)) == 600
