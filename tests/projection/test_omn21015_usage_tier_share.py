# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21015 AC4, table half: each usage row carries its model's tier and its share of that day's calls.

Jonah's ruling on OMN-21015 (2026-10-14T04:50Z): "the share is per day, and ... the usage call event should carry the
run's tier so the tier joins the row key". The ticket's falsifier: "the shares do not sum to 100".

Failure modes, each asserted below:
U1  one model under two tiers on one day is folded into one row;
U2  a day's shares do not sum to 1, or are not each row's calls over the day's calls;
U3  a new call leaves the other rows' shares of that day stale;
U4  another day's or another tenant's calls enter a share;
U5  an event with no tier is dropped or keyed under an empty name instead of not_tier_routed;
U6  a replayed call changes a count or a share;
U7  the served exposure lacks the tier or the share;
U8  a local store written before this change loses its rows or its cursor order when it is upgraded, or its
    kept rows' shares are not set by the day's next call.
U9  the deployed writer locks one model's key instead of the tenant's day, recounts without the tier, or
    republishes only the recounted row.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote

import pytest
import yaml
from fastapi.testclient import TestClient

from omnimarket.models.model_usage_call_event import ModelUsageCallEvent
from omnimarket.nodes.node_local_dashboard_serve_effect.handlers.handler_local_dashboard_serve import (
    create_dashboard_app,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.nodes.node_projection_usage_by_model_day.handlers import (
    HandlerProjectionUsageByModelDay,
)
from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_usage_by_model_day_store import (
    apply_usage_call,
)
from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_usage_by_model_day_writer import (
    UsageByModelDayProjectionWriter,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

_TENANT = "0af28cd0-0654-42e0-a050-358ee8240940"
_OTHER = "7c1e2b40-3a4f-4b6d-9e8a-112233445566"
_USAGE = "onex.snapshot.projection.usage-by-model-day.v1"
_CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "src/omnimarket/nodes/node_projection_usage_by_model_day/contract.yaml"
)


def _call(
    call_id: str,
    model: str,
    tier: str | None,
    *,
    day: str = "2026-10-14",
    tenant: str = _TENANT,
) -> ModelUsageCallEvent:
    fields: dict[str, object] = {
        "call_id": call_id,
        "model_name": model,
        "tenant_id": tenant,
        "prompt_tokens": 100,
        "completion_tokens": 20,
        "estimated_cost_usd": 0.0,
        "usage_source": "measured",
        "timestamp": datetime.fromisoformat(f"{day}T05:00:00+00:00"),
    }
    if tier is not None:
        fields["cost_tier_name"] = tier
    return ModelUsageCallEvent.model_validate(fields)


def _apply(db: SqliteDatabaseAdapter, *events: ModelUsageCallEvent) -> list[bool]:
    fold = HandlerProjectionUsageByModelDay()
    return [apply_usage_call(fold.handle(event), db) for event in events]


def _rows(
    db: SqliteDatabaseAdapter, day: str = "2026-10-14", tenant: str = _TENANT
) -> dict[tuple[str, str], dict[str, object]]:
    rows = db.query("usage_by_model_day", {"tenant_id": tenant, "usage_day": day})
    return {(str(r["model_id"]), str(r["cost_tier_name"])): r for r in rows}


@pytest.fixture
def db(tmp_path: Path) -> SqliteDatabaseAdapter:
    return SqliteDatabaseAdapter(tmp_path / "delegation.sqlite")


def test_u1_one_model_under_two_tiers_is_two_rows(db: SqliteDatabaseAdapter) -> None:
    _apply(db, _call("c1", "qwen", "local"), _call("c2", "qwen", "cheap_cloud"))
    assert sorted(_rows(db)) == [("qwen", "cheap_cloud"), ("qwen", "local")]


def test_u2_shares_are_calls_over_the_days_calls_and_sum_to_one(
    db: SqliteDatabaseAdapter,
) -> None:
    _apply(
        db,
        *(_call(f"q{i}", "qwen", "local") for i in range(3)),
        _call("g1", "gemini", "cheap_cloud"),
    )
    rows = _rows(db)
    assert rows[("qwen", "local")]["call_share"] == pytest.approx(0.75)
    assert rows[("gemini", "cheap_cloud")]["call_share"] == pytest.approx(0.25)
    assert sum(float(str(r["call_share"])) for r in rows.values()) == pytest.approx(1.0)


def test_u3_a_new_call_updates_every_share_of_that_day(
    db: SqliteDatabaseAdapter,
) -> None:
    _apply(
        db,
        *(_call(f"q{i}", "qwen", "local") for i in range(3)),
        _call("g1", "gemini", "cheap_cloud"),
    )
    _apply(db, _call("d1", "deepseek", "cheap_cloud"))
    rows = _rows(db)
    assert rows[("qwen", "local")]["call_share"] == pytest.approx(3 / 5)
    assert rows[("gemini", "cheap_cloud")]["call_share"] == pytest.approx(1 / 5)
    assert rows[("deepseek", "cheap_cloud")]["call_share"] == pytest.approx(1 / 5)


def test_u4_another_day_or_tenant_never_enters_a_share(
    db: SqliteDatabaseAdapter,
) -> None:
    _apply(
        db,
        _call("a1", "qwen", "local"),
        _call("b1", "qwen", "local", day="2026-10-13"),
        _call("b2", "gemini", "cheap_cloud", day="2026-10-13"),
        _call("o1", "gemini", "cheap_cloud", tenant=_OTHER),
    )
    assert _rows(db)[("qwen", "local")]["call_share"] == pytest.approx(1.0)
    assert _rows(db, day="2026-10-13")[("qwen", "local")][
        "call_share"
    ] == pytest.approx(0.5)


def test_u5_an_event_with_no_tier_is_not_tier_routed(db: SqliteDatabaseAdapter) -> None:
    _apply(db, _call("n1", "qwen", None), _call("n2", "qwen", ""))
    rows = _rows(db)
    assert sorted(rows) == [("qwen", "not_tier_routed")]
    assert rows[("qwen", "not_tier_routed")]["call_count"] == 2


def test_u6_a_replayed_call_changes_nothing(db: SqliteDatabaseAdapter) -> None:
    assert _apply(
        db, _call("r1", "qwen", "local"), _call("r2", "gemini", "cheap_cloud")
    ) == [True, True]
    before = {k: (v["call_count"], v["call_share"]) for k, v in _rows(db).items()}
    assert _apply(db, _call("r1", "qwen", "local")) == [False]
    assert {
        k: (v["call_count"], v["call_share"]) for k, v in _rows(db).items()
    } == before


def test_u7_the_served_exposure_carries_the_tier_and_the_share(
    db: SqliteDatabaseAdapter, tmp_path: Path
) -> None:
    _apply(db, _call("s1", "qwen", "local"), _call("s2", "gemini", "cheap_cloud"))
    topics = {
        cfg.topic: cfg
        for cfg in load_projection_exposures_from_contract(
            yaml.safe_load(_CONTRACT.read_text(encoding="utf-8")),
            "node_projection_usage_by_model_day",
            _CONTRACT,
        )
    }
    source = SqliteTableRowSource(tmp_path / "delegation.sqlite")
    client = TestClient(
        create_dashboard_app(
            handler=HandlerProjectionRead(topic_map=topics, row_source=source),
            tenant=_TENANT,
            topic_map=topics,
            row_source=source,
        )
    )
    response = client.get(f"/projection/{_USAGE}?tenant={quote(_TENANT)}")
    assert response.status_code == 200, response.json()
    served = {
        (r["model_id"], r["cost_tier_name"]): r["call_share"]
        for r in response.json()["rows"]
    }
    assert served == {
        ("qwen", "local"): pytest.approx(0.5),
        ("gemini", "cheap_cloud"): pytest.approx(0.5),
    }


_OLD_AGGREGATE_DDL = """
CREATE TABLE usage_by_model_day (
    tenant_id TEXT NOT NULL, usage_day TEXT NOT NULL, model_id TEXT NOT NULL,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cost_usd REAL NOT NULL,
    measured_cost_usd REAL, unmeasured_call_count INTEGER NOT NULL DEFAULT 0,
    call_count INTEGER NOT NULL, updated_at TEXT NOT NULL, projection_cursor INTEGER,
    PRIMARY KEY (tenant_id, usage_day, model_id)
)
"""


def test_u8_a_store_from_before_keeps_its_rows_and_cursor_order(tmp_path: Path) -> None:
    path = tmp_path / "delegation.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute(_OLD_AGGREGATE_DDL)
        conn.executemany(
            "INSERT INTO usage_by_model_day (tenant_id, usage_day, model_id, input_tokens, output_tokens, cost_usd, "
            "call_count, updated_at, projection_cursor) VALUES (?, '2026-10-12', ?, 10, 2, 0, ?, ?, ?)",
            [
                (_TENANT, "qwen", 3, datetime.now(UTC).isoformat(), 7),
                (_TENANT, "gemini", 1, datetime.now(UTC).isoformat(), 8),
            ],
        )
    db = SqliteDatabaseAdapter(path)
    rows = _rows(db, day="2026-10-12")
    assert sorted(rows) == [("gemini", "not_tier_routed"), ("qwen", "not_tier_routed")]
    assert [
        rows[k]["projection_cursor"]
        for k in (("qwen", "not_tier_routed"), ("gemini", "not_tier_routed"))
    ] == [7, 8]
    # Not computed before the day's next call, as on Postgres; that call sets every share of the day.
    assert {k: r["call_share"] for k, r in rows.items()} == dict.fromkeys(rows)
    _apply(db, _call("new", "qwen", "local", day="2026-10-12"))
    rows = _rows(db, day="2026-10-12")
    assert {k: r["call_share"] for k, r in rows.items()} == {
        ("qwen", "not_tier_routed"): pytest.approx(3 / 5),
        ("gemini", "not_tier_routed"): pytest.approx(1 / 5),
        ("qwen", "local"): pytest.approx(1 / 5),
    }
    assert min(int(str(r["projection_cursor"])) for r in rows.values()) > 8


def _writer_boundary() -> tuple[UsageByModelDayProjectionWriter, MagicMock]:
    writer = UsageByModelDayProjectionWriter()
    db = MagicMock()
    conn = MagicMock()
    db.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    conn.execute = AsyncMock()
    conn.fetch = AsyncMock()
    # The key-in-force check: True once the tier is in the key (OMN-21161).
    conn.fetchval = AsyncMock(return_value=True)
    writer._db = db
    writer.publish_snapshot_delta = AsyncMock()
    return writer, conn


def _day_row(model: str, tier: str, cursor: int) -> dict[str, object]:
    return {
        "tenant_id": _TENANT,
        "usage_day": date(2026, 10, 14),
        "model_id": model,
        "cost_tier_name": tier,
        "input_tokens": 1,
        "output_tokens": 1,
        "cost_usd": Decimal("0"),
        "measured_cost_usd": None,
        "unmeasured_call_count": 0,
        "call_count": 1,
        "call_share": 0.5,
        "updated_at": datetime(2026, 10, 14, tzinfo=UTC),
        "projection_cursor": cursor,
    }


async def test_u9_the_writer_locks_the_tenants_day_recounts_the_tier_and_republishes_the_day() -> (
    None
):
    """U9: two calls of one day on different models must not share out stale counts, and a deployed reader
    must receive every row whose share moved, not only the recounted one."""
    writer, conn = _writer_boundary()
    conn.fetch.side_effect = [
        [{"call_id": "w1"}],
        [],
        [_day_row("qwen", "local", 7), _day_row("gemini", "cheap_cloud", 8)],
    ]
    payload = {
        "call_id": "w1",
        "model_id": "qwen",
        "tenant_id": _TENANT,
        "cost_tier_name": "local",
        "created_at": "2026-10-14T05:00:00Z",
        "input_tokens": 1,
        "output_tokens": 1,
        "cost_usd": 0.0,
    }
    await writer.project_event(
        writer.topics[0],
        payload,
        MessageMeta(topic=writer.topics[0], partition=0, offset=1, fallback_id="f"),
    )
    statements = [
        (" ".join(str(c.args[0]).split()), c.args[1:])
        for c in conn.execute.await_args_list
    ]
    (lock,) = [args for sql, args in statements if "pg_advisory_xact_lock" in sql]
    assert json.loads(str(lock[0])) == ["usage_by_model_day", _TENANT, "2026-10-14"]
    (recount,) = [args for sql, args in statements if "GROUP BY" in sql]
    # Every tier of the model's day is recounted, not only the call's.
    assert recount == (_TENANT, date(2026, 10, 14), "qwen")
    published = [
        (c.kwargs["row"]["model_id"], c.kwargs["row"]["cost_tier_name"])
        for c in writer.publish_snapshot_delta.await_args_list
    ]
    assert published == [("qwen", "local"), ("gemini", "cheap_cloud")]
