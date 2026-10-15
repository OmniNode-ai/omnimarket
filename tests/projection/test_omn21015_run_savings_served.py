# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run savings survives the real local write and dashboard read boundaries.

Failures to catch before implementing storage: a missing served field; one
aggregate/legacy value copied onto every run; negative savings clamped; measured
zero confused with unknown; loss of baseline/manifest/classification; sparse
redelivery repricing different inputs; and another tenant's value being served.
The snapshot assertions drive the writer's real encoder, without a broker.
Postgres parity is a separate lab proof; these tests do not claim it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from omnimarket import pricing
from omnimarket.models.delegation.wire.model_quality_gate import ModelQualityGateResult
from omnimarket.nodes.node_local_dashboard_serve_effect.handlers.handler_local_dashboard_serve import (
    create_dashboard_app,
)
from omnimarket.nodes.node_metering_summary_compute import ModelCounterfactualBaseline
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
    ModelTaskDelegatedEvent,
)
from omnimarket.nodes.node_projection_metering_summary import (
    baseline as baseline_pricing,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection import sqlite_metering_summary
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.snapshot_publisher import ModelSnapshotDeltaMessage
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

TOPIC = "onex.snapshot.projection.delegation.decisions.v1"
TENANT = "00000000-0000-4000-8000-000000021015"
OTHER_TENANT = "00000000-0000-4000-8000-000000021016"
BASELINE = ModelCounterfactualBaseline(
    model="fixture-baseline",
    price_in_per_1k=Decimal("0.010"),
    price_out_per_1k=Decimal("0.020"),
    as_of="2026-10-13",
    pricing_manifest_version="fixture-v1",
    source="test_fixture",
)


class RecordingPublisher:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []

    def publish(self, message: ModelSnapshotDeltaMessage) -> bool:
        if message.topic == TOPIC:
            assert message.value is not None
            self.rows.append(json.loads(message.value)["row"])
        return True


@pytest.fixture
def priced(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin only the external pricing input. All computation, storage, exposure
    # discovery, response handling and snapshot encoding remain real.
    monkeypatch.setattr(
        pricing,
        "resolve_baseline_model",
        lambda **_: pricing.BaselineModelSelection(
            model=BASELINE.model,
            selection_case="fixed_default",
            state="RESOLVED",
            pricing_manifest_version=1,
        ),
    )
    monkeypatch.setattr(baseline_pricing, "resolve_baseline", lambda _: BASELINE)
    monkeypatch.setattr(
        sqlite_metering_summary,
        "resolve_metering_baseline_model",
        lambda: pricing.resolve_baseline_model(overlay={}, store={}),
    )


def store(tmp_path: Path) -> SqliteDatabaseAdapter:
    path = tmp_path / "runs.sqlite"
    with sqlite3.connect(path) as conn:
        # The local-deployment registry relation, which the delegation adapter
        # reads but does not create (same shape as the existing serving tests).
        conn.execute(
            "CREATE TABLE tenant_registry_mirror (tenant_slug TEXT NOT NULL UNIQUE, "
            "tenant_uuid TEXT NOT NULL, display_name TEXT, status TEXT NOT NULL, "
            "registry_created_at TEXT, observed_at TEXT NOT NULL, source_event_id TEXT)"
        )
        for slug, tenant in (
            ("savings-tenant", TENANT),
            ("other-tenant", OTHER_TENANT),
        ):
            conn.execute(
                "INSERT INTO tenant_registry_mirror "
                "(tenant_slug, tenant_uuid, status, observed_at) "
                "VALUES (?, ?, 'active', '2026-10-13T12:00:00+00:00')",
                (slug, tenant),
            )
    return SqliteDatabaseAdapter(path)


def event(index: int = 1, **changes: object) -> ModelTaskDelegatedEvent:
    payload: dict[str, object] = {
        "correlation_id": f"00000000-0000-4000-8000-{index:012d}",
        "tenant_id": "savings-tenant",
        "task_type": "document",
        "delegated_to": "fixture-serving-model",
        "model_name": "fixture-serving-model",
        "delegated_by": "test",
        "quality_gate_passed": True,
        "timestamp": "2026-10-13T12:00:00+00:00",
        "tokens_input": 1000,
        "tokens_output": 200,
        "cost_usd": 0.004,
        "cost_savings_usd": 999,
    }
    payload.update(changes)
    return ModelTaskDelegatedEvent.model_validate(payload)


def served(tmp_path: Path, tenant: str = TENANT) -> list[dict[str, object]]:
    topics = build_projection_topic_map()
    client = TestClient(
        create_dashboard_app(
            handler=HandlerProjectionRead(
                topic_map=topics,
                row_source=SqliteTableRowSource(tmp_path / "runs.sqlite"),
            ),
            tenant=tenant,
            topic_map=topics,
        )
    )
    response = client.get(f"/projection/{TOPIC}?tenant={tenant}")
    assert response.status_code == 200, response.text
    return response.json()["rows"]


@pytest.mark.usefixtures("priced")
@pytest.mark.parametrize(
    ("changes", "saving", "measurement"),
    [
        ({}, "0.010", "measured"),
        (
            {"tokens_input": 100, "tokens_output": 100, "cost_usd": 0.005},
            "-0.002",
            "measured",
        ),
        ({"cost_usd": 0.014}, "0.000", "measured"),
        ({"tokens_input": 0, "tokens_output": 0}, None, "unknown_tokens"),
    ],
)
def test_each_stored_served_and_republished_run_has_its_own_figure_and_basis(
    tmp_path: Path, changes: dict[str, object], saving: str | None, measurement: str
) -> None:
    db = store(tmp_path)
    publisher = RecordingPublisher()
    HandlerProjectionDelegation(publisher=publisher).project(event(**changes), db)
    stored = db.query("delegation_events")[0]
    rows = [stored, served(tmp_path)[0], publisher.rows[0]]
    for row in rows:
        actual = row["modelled_savings_usd"]
        assert (Decimal(str(actual)) if actual is not None else None) == (
            Decimal(saving) if saving is not None else None
        )
        assert row["savings_measurement"] == measurement
        assert row["savings_baseline_model"] == "fixture-baseline"
        assert row["savings_pricing_manifest_version"] == "fixture-v1"


@pytest.mark.usefixtures("priced")
def test_unresolved_baseline_serves_null_instead_of_the_legacy_saving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(baseline_pricing, "resolve_baseline", lambda _: None)
    publisher = RecordingPublisher()
    HandlerProjectionDelegation(publisher=publisher).project(event(), store(tmp_path))
    row = served(tmp_path)[0]
    assert row["modelled_savings_usd"] is None
    assert row["savings_baseline_model"] is None
    assert row["savings_pricing_manifest_version"] is None
    assert row["savings_measurement"] == "measured"


@pytest.mark.usefixtures("priced")
def test_a_stored_unknown_spend_is_not_priced_as_a_free_call(tmp_path: Path) -> None:
    db = store(tmp_path)
    cid = event().correlation_id
    db.upsert(
        "delegation_events",
        "correlation_id",
        {
            "correlation_id": cid,
            "tenant_id": TENANT,
            "created_at": "2026-10-13T12:00:00+00:00",
            "tokens_input": 1000,
            "tokens_output": 200,
            "cost_usd": None,
        },
    )
    HandlerProjectionDelegation(
        publisher=RecordingPublisher()
    ).project_quality_gate_result(
        ModelQualityGateResult(correlation_id=cid, passed=True, quality_score=1.0),
        db,
        tenant_identity="savings-tenant",
        event_timestamp=datetime(2026, 10, 13, 12, tzinfo=UTC),
    )
    row = served(tmp_path)[0]
    assert row["cost_usd"] is None
    assert row["modelled_savings_usd"] is None
    assert row["savings_measurement"] == "unknown_spend"


@pytest.mark.usefixtures("priced")
def test_a_sparse_redelivery_keeps_the_figure_paired_with_the_preserved_inputs(
    tmp_path: Path,
) -> None:
    db = store(tmp_path)
    handler = HandlerProjectionDelegation(publisher=RecordingPublisher())
    handler.project(event(), db)
    handler.project(event(tokens_input=0, tokens_output=0, cost_usd=0), db)
    row = served(tmp_path)[0]
    assert Decimal(str(row["modelled_savings_usd"])) == Decimal("0.010")
    assert row["savings_measurement"] == "measured"


@pytest.mark.usefixtures("priced")
def test_tenant_scoping_does_not_serve_another_tenants_saving(tmp_path: Path) -> None:
    db = store(tmp_path)
    handler = HandlerProjectionDelegation(publisher=RecordingPublisher())
    handler.project(event(), db)
    handler.project(event(2, tenant_id="other-tenant", cost_usd=0.005), db)
    first = served(tmp_path)
    other = served(tmp_path, OTHER_TENANT)
    assert len(first) == len(other) == 1
    assert first[0]["correlation_id"] != other[0]["correlation_id"]
    assert Decimal(str(first[0]["modelled_savings_usd"])) == Decimal("0.010")
    assert Decimal(str(other[0]["modelled_savings_usd"])) == Decimal("0.009")


@pytest.mark.usefixtures("priced")
def test_run_and_headline_share_the_named_baseline_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed local selection must reach the stored and served run basis."""
    selected = pricing.BaselineModelSelection(
        model="changed-local-baseline",
        selection_case="fixed_default",
        state="RESOLVED",
        pricing_manifest_version=1,
    )
    monkeypatch.setattr(
        sqlite_metering_summary, "resolve_metering_baseline_model", lambda: selected
    )
    monkeypatch.setattr(
        baseline_pricing,
        "resolve_baseline",
        lambda model: BASELINE.model_copy(update={"model": model}),
    )
    db = store(tmp_path)
    HandlerProjectionDelegation(publisher=RecordingPublisher()).project(event(), db)
    for row in (db.query("delegation_events")[0], served(tmp_path)[0]):
        assert row["savings_baseline_model"] == selected.model
        assert Decimal(str(row["modelled_savings_usd"])) == Decimal("0.010")
