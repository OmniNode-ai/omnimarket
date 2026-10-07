# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19970: the dev and demo seed publishes labelled fixture rows through the
real delegation projection, and measured savings exclude them by default.

Failure modes pinned here (spec: workflow/records/plans/OMN-19970):
  1. a second seed duplicates rows;
  2. a fixture row loses its label on the way to ``delegation_events``;
  3. a real row is labelled ``fixture``, or an untagged row is not ``real``;
  4. fixture savings leak into ``onex metering``'s measured total;
  5. the seed writes around the projection instead of through it.
The Postgres half (the migration and the summary view) is in
``test_omn19970_fixture_data_source_real_postgres.py``.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_dev_seed_effect.handlers.handler_dev_seed import (
    HandlerDevSeed,
)
from omnimarket.nodes.node_local_dashboard_serve_effect.handlers.handler_local_dashboard_serve import (
    create_dashboard_app,
)
from omnimarket.nodes.node_metering_summary_compute.models.model_metering_summary import (
    ModelCounterfactualBaseline,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection import sqlite_metering_summary
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.envelope import (
    DATA_SOURCE_FIXTURE,
    DATA_SOURCE_REAL,
    envelope_data_source,
    strip_runner_injected_keys,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.projection.sqlite_metering_reader import read_metering_records
from tests.helpers.tenant_registry import (
    PROJECTION_TENANT_SLUG,
    PROJECTION_TENANT_UUID,
    seed_tenant_registry,
)

pytestmark = pytest.mark.unit


class _NullPublisher:
    def publish(self, *args: object, **kwargs: object) -> bool:
        return True


def _terminal(
    correlation_id: str, savings: float
) -> ModelDelegateSkillTerminalProjection:
    return ModelDelegateSkillTerminalProjection.from_payload(
        {
            "tenant_id": PROJECTION_TENANT_SLUG,
            "status": "completed",
            "correlation_id": correlation_id,
            "task_type": "document",
            "provider": "local-qwen",
            "model_name": "Qwen3.8-27B",
            "quality_gate_passed": True,
            "metrics": {
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "latency_ms": 900,
                "cost_usd": 0.0,
                "cost_savings_usd": savings,
            },
        }
    )


# --- failure modes 2 and 3: the label on the sync writer and on the envelope


def test_sync_writer_labels_a_fixture_row_and_defaults_to_real() -> None:
    handler = HandlerProjectionDelegation(publisher=_NullPublisher())
    db = InmemoryDatabaseAdapter()
    seed_tenant_registry(db)
    fixture, real = str(uuid4()), str(uuid4())
    handler.project_delegate_skill_terminal(
        _terminal(fixture, 0.5), db, data_source=DATA_SOURCE_FIXTURE
    )
    handler.project_delegate_skill_terminal(_terminal(real, 0.1), db)
    rows = {r["correlation_id"]: r for r in db.query("delegation_events")}
    assert rows[fixture]["data_source"] == "fixture"
    assert rows[real]["data_source"] == "real"


def test_sync_writer_refuses_an_unknown_data_source() -> None:
    handler = HandlerProjectionDelegation(publisher=_NullPublisher())
    with pytest.raises(ValueError, match="data_source"):
        handler.project_delegate_skill_terminal(
            _terminal(str(uuid4()), 0.1), InmemoryDatabaseAdapter(), data_source="demo"
        )


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"_envelope": {"metadata": {"tags": {"data_source": "fixture"}}}}, "fixture"),
        ({"_envelope": {"metadata": {"tags": {"data_source": "real"}}}}, "real"),
        # An unknown tag is not a fixture: only the exact label marks one.
        ({"_envelope": {"metadata": {"tags": {"data_source": "bogus"}}}}, "real"),
        ({"_envelope": {"metadata": {"tags": {}}}}, "real"),
        ({"_envelope": {"metadata": None}}, "real"),
        ({"_envelope": "not-a-dict"}, "real"),
        ({}, "real"),
    ],
)
def test_envelope_data_source_reads_only_the_exact_fixture_tag(
    data: dict[str, Any], expected: str
) -> None:
    assert envelope_data_source(data) == expected
    assert DATA_SOURCE_REAL == "real"


def test_envelope_keys_are_still_stripped_before_the_wire_model() -> None:
    data = {
        "status": "completed",
        "_envelope": {"metadata": {"tags": {"data_source": "fixture"}}},
    }
    assert "_envelope" not in strip_runner_injected_keys(data)


# --- failure modes 1 and 5: the seed goes through the projection, idempotently


def test_seeding_twice_leaves_the_same_rows_all_labelled_fixture() -> None:
    db = InmemoryDatabaseAdapter()
    seed_tenant_registry(db)
    seed = HandlerDevSeed()
    first = seed.seed_local(db, tenant_id=PROJECTION_TENANT_SLUG)
    after_first = db.query("delegation_events")
    second = seed.seed_local(db, tenant_id=PROJECTION_TENANT_SLUG)
    after_second = db.query("delegation_events")

    assert first.rows_projected == len(after_first) > 0
    assert len(after_second) == len(after_first)
    assert first.correlation_ids == second.correlation_ids
    assert {r["data_source"] for r in after_second} == {"fixture"}
    # Deterministic, well-formed ids: the upsert key is what makes a re-seed a no-op.
    for correlation_id in first.correlation_ids:
        UUID(correlation_id)


def test_fixture_set_carries_both_outcomes_so_runs_page_shows_each() -> None:
    db = InmemoryDatabaseAdapter()
    seed_tenant_registry(db)
    HandlerDevSeed().seed_local(db, tenant_id=PROJECTION_TENANT_SLUG)
    rows = db.query("delegation_events")
    assert {r["terminal_ok"] for r in rows} == {True, False}


@pytest.mark.parametrize(
    "topic",
    [
        "onex.snapshot.projection.delegation.decisions.v1",
        "onex.snapshot.projection.delegation.correlation-trace.v1",
    ],
)
def test_fixture_label_reaches_each_delegation_exposure(
    tmp_path: Path, topic: str
) -> None:
    db, path = _sqlite_store(tmp_path)
    seeded = HandlerDevSeed().seed_local(db, tenant_id=PROJECTION_TENANT_SLUG)
    topics = build_projection_topic_map()
    handler = HandlerProjectionRead(
        topic_map=topics, row_source=SqliteTableRowSource(path)
    )
    client = TestClient(
        create_dashboard_app(
            handler=handler, topic_map=topics, tenant=str(PROJECTION_TENANT_UUID)
        )
    )
    response = client.get(f"/projection/{topic}")
    assert response.status_code == 200, response.json()
    rows = response.json()["rows"]
    assert {r["correlation_id"] for r in rows} == set(seeded.correlation_ids)
    assert {r.get("data_source") for r in rows} == {"fixture"}


def test_every_delegation_row_exposure_declares_fixture_provenance() -> None:
    exposures = [
        cfg
        for cfg in build_projection_topic_map().values()
        if cfg.table == "delegation_events"
    ]
    assert len(exposures) == 4
    assert all("data_source" in cfg.columns for cfg in exposures)


# --- failure mode 4: measured savings exclude fixture rows by default


def _sqlite_store(tmp_path: Path) -> tuple[SqliteDatabaseAdapter, Path]:
    path = tmp_path / "delegation.sqlite"
    db = SqliteDatabaseAdapter(path)
    seed_tenant_registry(db)
    return db, path


def test_metering_excludes_fixture_rows_unless_asked(tmp_path: Path) -> None:
    db, path = _sqlite_store(tmp_path)
    handler = HandlerProjectionDelegation(publisher=_NullPublisher())
    real = str(uuid4())
    handler.project_delegate_skill_terminal(_terminal(real, 0.25), db)
    seeded = HandlerDevSeed().seed_local(db, tenant_id=PROJECTION_TENANT_SLUG)

    measured = read_metering_records(db_path=path)
    assert [r.correlation_id for r in measured] == [real]

    everything = read_metering_records(db_path=path, include_fixtures=True)
    assert {r.correlation_id for r in everything} == {real, *seeded.correlation_ids}


def test_overview_measured_savings_are_unchanged_by_seeding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db, path = _sqlite_store(tmp_path)
    HandlerProjectionDelegation(
        publisher=_NullPublisher()
    ).project_delegate_skill_terminal(_terminal(str(uuid4()), 0.25), db)
    baseline = ModelCounterfactualBaseline(
        model="test-baseline",
        price_in_per_1k=Decimal("1"),
        price_out_per_1k=Decimal("2"),
        as_of="2026-09-28",
        pricing_manifest_version="1",
        source="test_manifest",
    )
    monkeypatch.setattr(sqlite_metering_summary, "resolve_baseline", lambda _: baseline)
    tenant = str(PROJECTION_TENANT_UUID)
    now = datetime.now(UTC) + timedelta(seconds=1)

    def refresh(*, include_fixtures: bool = False) -> None:
        sqlite_metering_summary.refresh_metering_summary(
            path, tenant, baseline.model, now, include_fixtures=include_fixtures
        )

    topics = build_projection_topic_map()
    handler = HandlerProjectionRead(
        topic_map=topics, row_source=SqliteTableRowSource(path)
    )
    client = TestClient(
        create_dashboard_app(handler=handler, topic_map=topics, tenant=tenant)
    )

    def overview() -> dict[str, Any]:
        response = client.get(
            "/projection/onex.snapshot.projection.metering-summary.v1"
        )
        assert response.status_code == 200, response.json()
        return next(r for r in response.json()["rows"] if r["window_kind"] == "all")

    refresh()
    before = overview()
    assert before["runs_measured"] == 1
    assert Decimal(before["savings_usd"]) == Decimal("0.14")
    HandlerDevSeed().seed_local(db, tenant_id=PROJECTION_TENANT_SLUG)
    refresh()
    after = overview()
    assert after["runs_measured"] == before["runs_measured"]
    assert after["savings_usd"] == before["savings_usd"]
    # Positive control: these fixtures can contribute when explicitly included.
    refresh(include_fixtures=True)
    included = overview()
    assert included["runs_measured"] > after["runs_measured"]
    assert Decimal(included["savings_usd"]) > Decimal(after["savings_usd"])


def test_metering_reads_a_store_written_before_the_label_existed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE delegation_events (correlation_id TEXT PRIMARY KEY, created_at TEXT, "
        "delegated_to TEXT, model_name TEXT, task_type TEXT, tokens_input INTEGER, "
        "tokens_output INTEGER, cost_usd REAL, cost_savings_usd REAL)"
    )
    legacy = str(uuid4())
    conn.execute(
        "INSERT INTO delegation_events VALUES (?, '2026-09-28T10:00:00+00:00', 'm', 'm', "
        "'document', 10, 2, 0.0, 0.01)",
        (legacy,),
    )
    conn.commit()
    conn.close()
    assert [r.correlation_id for r in read_metering_records(db_path=path)] == [legacy]
