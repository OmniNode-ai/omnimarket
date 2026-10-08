# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex dashboard`` never lists a topic ``ok`` that it then answers 503 (OMN-20709).

The local catalogue copied each exposure's contract status, so a topic the
local store has no relation for was still listed ``ok, bus_backed: true`` and
every read of it answered 503 ``projection_table_missing``. The page trusted
the listing and showed those panels as broken reads. The catalogue now asks the
row source which topics it can serve and lists the rest ``degraded``.

The store here is the one a fresh install has after one ``onex delegate``: a
SQLite file opened by the writer's adapter, holding one delegation row. The
topic map is the real, contract-declared one, so a new exposure that the local
store cannot serve is caught by the next run of this file.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from omnimarket.nodes.node_local_dashboard_serve_effect.handlers.handler_local_dashboard_serve import (
    create_dashboard_app,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

_TENANT = "0af28cd0-0654-42e0-a050-358ee8240940"
_SUMMARY = "onex.snapshot.projection.delegation.summary.v1"
# On a fresh local store nothing creates this relation; on Postgres it is a view.
_UNSERVABLE = "onex.snapshot.projection.delegation.model-routing.v1"


def _fresh_install_store(tmp_path: Path) -> Path:
    path = tmp_path / "delegation.sqlite"
    SqliteDatabaseAdapter(path).upsert(
        "delegation_events",
        "correlation_id",
        {
            "correlation_id": "b4030008-5873-4569-8ecc-98cd7247f751",
            "tenant_id": _TENANT,
            "task_type": "reasoning",
            "delegated_to": "gemini-3.5-flash-lite",
            "quality_gate_passed": True,
            "latency_ms": 938,
            "data_source": "real",
            "created_at": "2026-10-08T03:11:00+00:00",
        },
    )
    return path


def _client(tmp_path: Path, *, probe: bool = True) -> TestClient:
    topics = build_projection_topic_map()
    source = SqliteTableRowSource(_fresh_install_store(tmp_path))
    return TestClient(
        create_dashboard_app(
            handler=HandlerProjectionRead(topic_map=topics, row_source=source),
            tenant=_TENANT,
            topic_map=topics,
            row_source=source if probe else None,
        )
    )


def _read(client: TestClient, topic: str) -> int:
    return client.get(f"/projection/{topic}?tenant={quote(_TENANT)}").status_code


def test_no_topic_listed_ok_answers_503(tmp_path: Path) -> None:
    client = _client(tmp_path)
    listed_ok = [
        row["topic"]
        for row in client.get("/projections").json()["topics"]
        if row["status"] == "ok"
    ]
    assert listed_ok
    statuses = {topic: _read(client, topic) for topic in listed_ok}
    assert {topic: code for topic, code in statuses.items() if code == 503} == {}


def test_without_the_probe_the_catalogue_lists_topics_it_cannot_serve(
    tmp_path: Path,
) -> None:
    """Positive control: the property above is not vacuous on this store."""
    client = _client(tmp_path, probe=False)
    listed_ok = [
        row["topic"]
        for row in client.get("/projections").json()["topics"]
        if row["status"] == "ok"
    ]
    assert any(_read(client, topic) == 503 for topic in listed_ok)


def test_an_unservable_topic_is_listed_degraded_with_the_stores_reason(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    row = next(
        row
        for row in client.get("/projections").json()["topics"]
        if row["topic"] == _UNSERVABLE
    )
    assert row["status"] == "degraded"
    assert row["backing"] == "not_in_local_store"
    assert row["degraded_reason"] == "projection_table_missing"
    # The listing changes; the read still refuses by the same name.
    assert _read(client, _UNSERVABLE) == 503


def test_the_summary_is_listed_ok_and_served(tmp_path: Path) -> None:
    client = _client(tmp_path)
    row = next(
        row
        for row in client.get("/projections").json()["topics"]
        if row["topic"] == _SUMMARY
    )
    assert row["status"] == "ok"
    assert row["backing"] == "bus"
    response = client.get(f"/projection/{_SUMMARY}?tenant={quote(_TENANT)}")
    assert response.status_code == 200
    assert response.json()["rows"][0]["totalDelegations"] >= 1


@pytest.mark.parametrize("probe", [True, False])
def test_a_contract_degraded_topic_keeps_its_own_reason(
    tmp_path: Path, probe: bool
) -> None:
    rows = _client(tmp_path, probe=probe).get("/projections").json()["topics"]
    for row in rows:
        if not row["bus_backed"]:
            assert row["backing"] == "not_yet_bus_backed"
            assert row["status"] == "degraded"
