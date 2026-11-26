# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-25861: each usage-by-model-day row serves the backends its calls were reached through.

usage-by-model-day.v1 grouped calls by the model called and served no backend, so the Usage page could not show that
a model was reached through an alias such as byok-reflection, while every other local per-model figure serves one
since omnimarket#6442. A row's ``backends`` follows that change's rule (delegation_model_routing_view_v4.sql): the
sorted distinct aliases its calls were reached through, where the alias is set and differs from the model, so a
direct call adds nothing. A call whose backend was never stated is unknown, and a row counting one serves NULL, not
a list that claims to be complete (JG-19).

The ticket's falsifier: two calls to one model, one through byok-reflection, fails unless the row serves
["byok-reflection"].

Failure modes, each asserted below (the local store; the Postgres writer's twin is
tests/test_omn25861_usage_backends_real_postgres.py):
B1  two calls to one model, one through byok-reflection, serve no backends, or not exactly ["byok-reflection"],
    on the row the local dashboard serves (AC1);
B2  a direct call is listed as a backend: one whose backend is the model itself, or empty;
B3  one backend reached twice is listed twice, or the backends are not in code-point order (the Postgres
    writer's COLLATE "C" order, so both stores serve one order);
B4  a call whose backend was never stated is served as a known list (empty or partial) instead of NULL;
B5  one tier's backends leak into another tier's row of the same model, or another day's or tenant's into a row;
B6  a local store written before this change cannot be opened, loses rows, or is not given the new columns once;
B7  the event drops a stated backend on its way to the stored call, or invents one the event did not state;
B8  the store-neutral write names a column its database does not have yet: an overlay-bound local install writes
    through the Postgres sync adapter to a database 0004 may not have reached (or has half reached), and every usage
    call would be refused there and dropped by the producer's guard.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
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
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.protocol_database import (
    DatabaseAdapter,
    InmemoryDatabaseAdapter,
)
from omnimarket.projection.sqlite_database import (
    SqliteDatabaseAdapter,
    reconcile_existing_store,
)

_TENANT = "0af28cd0-0654-42e0-a050-358ee8240940"
_OTHER = "7c1e2b40-3a4f-4b6d-9e8a-112233445566"
_USAGE = "onex.snapshot.projection.usage-by-model-day.v1"
_BEAM = "Beam-1.0-501B-A23B"
_CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "src/omnimarket/nodes/node_projection_usage_by_model_day/contract.yaml"
)
_UNSET = object()


def _call(
    call_id: str,
    backend: object = _UNSET,
    *,
    model: str = _BEAM,
    tier: str = "cheap_cloud",
    day: str = "2026-11-19",
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
        "cost_tier_name": tier,
        "timestamp": datetime.fromisoformat(f"{day}T05:00:00+00:00"),
    }
    if backend is not _UNSET:
        fields["backend"] = backend
    return ModelUsageCallEvent.model_validate(fields)


def _apply(db: DatabaseAdapter, *events: ModelUsageCallEvent) -> None:
    fold = HandlerProjectionUsageByModelDay()
    for event in events:
        assert apply_usage_call(fold.handle(event), db)


def _served(path: Path) -> list[dict[str, object]]:
    """The usage rows exactly as the local dashboard serves them to the Usage page."""
    topics = {
        cfg.topic: cfg
        for cfg in load_projection_exposures_from_contract(
            yaml.safe_load(_CONTRACT.read_text(encoding="utf-8")),
            "node_projection_usage_by_model_day",
            _CONTRACT,
        )
    }
    source = SqliteTableRowSource(path)
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
    rows: list[dict[str, object]] = response.json()["rows"]
    return rows


def _backends(path: Path) -> dict[tuple[str, str, str], object]:
    return {
        (str(r["usage_day"]), str(r["model_id"]), str(r["cost_tier_name"])): r[
            "backends"
        ]
        for r in _served(path)
    }


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "delegation.sqlite"


@pytest.fixture
def db(path: Path) -> SqliteDatabaseAdapter:
    return SqliteDatabaseAdapter(path)


def test_b1_a_model_reached_through_byok_reflection_serves_it(
    db: SqliteDatabaseAdapter, path: Path
) -> None:
    """AC1's falsifier: two calls to one model, one through byok-reflection, the other direct."""
    _apply(db, _call("c1", "byok-reflection"), _call("c2", _BEAM))
    (row,) = _served(path)
    assert row["model_id"] == _BEAM
    assert row["call_count"] == 2
    assert row["backends"] == ["byok-reflection"]


def test_b2_a_direct_call_adds_no_backend(
    db: SqliteDatabaseAdapter, path: Path
) -> None:
    # The model itself, or an empty alias, is a call that reached the model directly.
    _apply(db, _call("d1", _BEAM), _call("d2", ""))
    assert _backends(path) == {("2026-11-19", _BEAM, "cheap_cloud"): []}


def test_b3_backends_are_distinct_and_sorted(
    db: SqliteDatabaseAdapter, path: Path
) -> None:
    _apply(
        db,
        _call("s1", "byok-reflection"),
        _call("s2", "byok-openrouter"),
        _call("s3", "byok-reflection"),
    )
    assert _backends(path) == {
        ("2026-11-19", _BEAM, "cheap_cloud"): ["byok-openrouter", "byok-reflection"]
    }
    # Code point, not a linguistic order that skips the hyphen: "cloud-glm" first.
    _apply(
        db,
        _call("s4", "cloudflare-x", tier="local"),
        _call("s5", "cloud-glm", tier="local"),
    )
    assert _backends(path)[("2026-11-19", _BEAM, "local")] == [
        "cloud-glm",
        "cloudflare-x",
    ]


def test_b4_a_call_with_no_stated_backend_makes_the_row_unknown(
    db: SqliteDatabaseAdapter, path: Path
) -> None:
    # One call says nothing about its backend: the row cannot know its whole list, so it serves NULL rather than
    # ["byok-reflection"] as if that were every backend, or [] as if every call were direct.
    _apply(db, _call("u1", "byok-reflection"), _call("u2"))
    assert _backends(path) == {("2026-11-19", _BEAM, "cheap_cloud"): None}
    # A row whose calls state nothing at all is unknown too, never an empty list.
    _apply(db, _call("u3", model="qwen", tier="local"))
    assert _backends(path)[("2026-11-19", "qwen", "local")] is None


def test_b5_each_row_lists_only_its_own_calls_backends(
    db: SqliteDatabaseAdapter, path: Path
) -> None:
    _apply(
        db,
        _call("t1", "byok-reflection", tier="cheap_cloud"),
        _call("t2", "local-coder", tier="local"),
        _call("t3", "byok-openrouter", day="2026-11-20"),
        _call("t4", "byok-glm-general", tenant=_OTHER),
    )
    assert _backends(path) == {
        ("2026-11-19", _BEAM, "cheap_cloud"): ["byok-reflection"],
        ("2026-11-19", _BEAM, "local"): ["local-coder"],
        ("2026-11-20", _BEAM, "cheap_cloud"): ["byok-openrouter"],
    }


def test_b7_the_stored_call_keeps_exactly_the_stated_backend(
    db: SqliteDatabaseAdapter, path: Path
) -> None:
    _apply(db, _call("k1", "byok-reflection"), _call("k2", _BEAM), _call("k3"))
    conn = sqlite3.connect(path)
    try:
        stored = dict(
            conn.execute(
                "SELECT call_id, backend FROM usage_by_model_day_calls"
            ).fetchall()
        )
    finally:
        conn.close()
    assert stored == {"k1": "byok-reflection", "k2": _BEAM, "k3": None}


# A store as the build before this change leaves it: the tier key and the share, no backend columns.
_OLD_CALLS_DDL = """
CREATE TABLE usage_by_model_day_calls (
    call_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, usage_day TEXT NOT NULL, model_id TEXT NOT NULL,
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cost_usd REAL NOT NULL,
    occurred_at TEXT NOT NULL, ingested_at TEXT NOT NULL,
    usage_source TEXT NOT NULL DEFAULT 'unknown', cost_tier_name TEXT NOT NULL DEFAULT 'not_tier_routed'
)
"""
_OLD_AGGREGATE_DDL = """
CREATE TABLE usage_by_model_day (
    tenant_id TEXT NOT NULL, usage_day TEXT NOT NULL, model_id TEXT NOT NULL,
    cost_tier_name TEXT NOT NULL DEFAULT 'not_tier_routed',
    input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL, cost_usd REAL NOT NULL,
    measured_cost_usd REAL, unmeasured_call_count INTEGER NOT NULL DEFAULT 0,
    call_count INTEGER NOT NULL, updated_at TEXT NOT NULL, projection_cursor INTEGER, call_share REAL,
    PRIMARY KEY (tenant_id, usage_day, model_id, cost_tier_name)
)
"""


def test_b6_a_store_from_before_gains_the_columns_and_keeps_its_rows(
    path: Path,
) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(_OLD_CALLS_DDL)
        conn.execute(_OLD_AGGREGATE_DDL)
        conn.execute(
            "INSERT INTO usage_by_model_day_calls VALUES "
            "('old', ?, '2026-11-18', ?, 7, 8, 0.5, '2026-11-18T05:00:00+00:00', "
            "'2026-11-18T05:00:01+00:00', 'measured', 'cheap_cloud')",
            (_TENANT, _BEAM),
        )
        conn.execute(
            "INSERT INTO usage_by_model_day VALUES "
            "(?, '2026-11-18', ?, 'cheap_cloud', 7, 8, 0.5, 0.5, 0, 1, "
            "'2026-11-18T05:00:01+00:00', 1, 1.0)",
            (_TENANT, _BEAM),
        )
        conn.commit()
    finally:
        conn.close()

    # The local dashboard's own upgrade before it serves a store (resolve_local_row_source).
    reconcile_existing_store(path)
    # The old row is kept and readable; nothing recorded its backends, so it serves NULL.
    assert _backends(path) == {("2026-11-18", _BEAM, "cheap_cloud"): None}
    (old,) = [r for r in _served(path) if r["usage_day"] == "2026-11-18"]
    assert (old["input_tokens"], old["output_tokens"], old["call_count"]) == (7, 8, 1)
    # A new day's calls on the upgraded store serve their backends.
    db = SqliteDatabaseAdapter(path)
    _apply(db, _call("n1", "byok-reflection"), _call("n2", _BEAM))
    assert _backends(path)[("2026-11-19", _BEAM, "cheap_cloud")] == ["byok-reflection"]
    # The step ran once: upgrading again applies nothing twice and keeps every row.
    reconcile_existing_store(path)
    assert len(_served(path)) == 2
    conn = sqlite3.connect(path)
    try:
        (stored,) = conn.execute(
            "SELECT backends FROM usage_by_model_day WHERE usage_day = '2026-11-19'"
        ).fetchone()
    finally:
        conn.close()
    assert json.loads(stored) == ["byok-reflection"]


class _ColumnsInForce(InmemoryDatabaseAdapter):
    """A store that, like Postgres, has only the 0004 columns a migration added, and refuses any other."""

    def __init__(self, columns: set[tuple[str, str]]) -> None:
        super().__init__()
        self._columns = columns

    def has_column(self, table: str, column: str) -> bool:
        return (table, column) in self._columns

    def upsert(self, table: str, conflict_key: str, row: dict[str, object]) -> bool:
        for column in ("backend", "backends"):
            if column in row and (table, column) not in self._columns:
                raise AssertionError(f"{table}.{column} does not exist yet")
        return super().upsert(table, conflict_key, row)


@pytest.mark.parametrize(
    ("columns", "call_backend", "row_backends"),
    [
        (set(), "absent", "absent"),
        ({("usage_by_model_day_calls", "backend")}, "byok-reflection", "absent"),
        (
            {
                ("usage_by_model_day_calls", "backend"),
                ("usage_by_model_day", "backends"),
            },
            "byok-reflection",
            ["byok-reflection"],
        ),
    ],
    ids=["before-0004", "half-applied-0004", "with-0004"],
)
def test_b8_the_store_writes_each_new_column_only_where_it_exists(
    columns: set[tuple[str, str]], call_backend: object, row_backends: object
) -> None:
    db = _ColumnsInForce(columns)
    _apply(db, _call("p1", "byok-reflection"))
    (call,) = db.query("usage_by_model_day_calls")
    (row,) = db.query("usage_by_model_day")
    assert call.get("backend", "absent") == call_backend
    if "backends" in row:
        stored = row["backends"]
        assert (
            json.loads(stored) if isinstance(stored, str) else stored
        ) == row_backends
    else:
        assert row_backends == "absent"
