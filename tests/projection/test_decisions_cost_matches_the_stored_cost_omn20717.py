# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The decisions exposure serves the cost the store holds, in both serving modes.

The Usage page's cost per model and day is a sum over calls. It can be checked
against the call rows only if the per-call exposure serves each call's own
cost, unchanged. The column was added to the exposure earlier with a test whose
measured cost is exactly 0.0, which shows the column is present but cannot tell
a served value from a constant zero. No test read the bus-snapshot path.

Failure modes, each with a test below:
  F1  a non-zero stored cost is served as another value (zeroed, rounded or
      dropped) through the local store and the real dashboard app (mode 1);
  F2  the served costs of a day do not add up to the stored costs, so a sum
      over the call rows cannot be compared with the Usage page's figure;
  F3  a row with no cost is counted as 0 in that sum instead of left out;
  F4  the row republished to the bus snapshot (mode 2) does not carry the cost
      the store returned, or carries the writer's own value in its place.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from omnimarket.nodes.node_local_dashboard_serve_effect.handlers.handler_local_dashboard_serve import (
    create_dashboard_app,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
    ModelTaskDelegatedEvent,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.snapshot_publisher import ModelSnapshotDeltaMessage
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.projection.tenant_registry_resolution import (
    TENANT_REGISTRY_MIRROR_TABLE,
)

_TENANT = "bc08ff01-9d50-442c-aca1-7ad2e687db70"
_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"
_PRICED = "0b6f5c1e-6c0f-4d53-9a55-6f0a7c5c2a11"
_PRICED_AGAIN = "1c7a6d2f-7d10-4e64-8b66-7a1b8d6d3b22"
_FREE = "2d8b7e30-8e21-4f75-9c77-8b2c9e7e4c33"
_NO_COST = "3e9c8f41-9f32-4086-8d88-9c3daf8f5d44"

# What each call cost. The two priced values are not round, so a served value
# that was rounded to cents or truncated to an integer cannot equal them.
_COSTS: dict[str, float | None] = {
    _PRICED: 0.0123,
    _PRICED_AGAIN: 0.000457,
    _FREE: 0.0,
    _NO_COST: None,
}


def _store(tmp_path: Path) -> Path:
    path = tmp_path / "delegation.sqlite"
    adapter = SqliteDatabaseAdapter(path)
    for index, (correlation_id, cost) in enumerate(_COSTS.items()):
        written_at = f"2026-10-12T09:00:0{index}+00:00"
        adapter.upsert(
            "delegation_events",
            "correlation_id",
            {
                "correlation_id": correlation_id,
                "tenant_id": _TENANT,
                "task_type": "document",
                "delegated_to": "provider-model",
                "quality_gate_passed": True,
                "quality_gate_detail": "completed",
                "terminal_ok": True,
                "cost_usd": cost,
                "data_source": "real",
                "created_at": written_at,
                "written_at": written_at,
            },
        )
    return path


# Built once: discovering every contract's exposures takes seconds.
_TOPICS = build_projection_topic_map()


def _served_and_stored(
    tmp_path: Path,
) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    """The rows the dashboard app serves, and the costs read from the same file."""
    path = _store(tmp_path)
    client = TestClient(
        create_dashboard_app(
            handler=HandlerProjectionRead(
                topic_map=_TOPICS, row_source=SqliteTableRowSource(path)
            ),
            tenant=_TENANT,
            topic_map=_TOPICS,
        )
    )
    response = client.get(f"/projection/{_DECISIONS}?tenant={quote(_TENANT)}")
    assert response.status_code == 200, response.text
    served = {str(row["correlation_id"]): row for row in response.json()["rows"]}
    stored = {
        str(row["correlation_id"]): row["cost_usd"]
        for row in SqliteDatabaseAdapter(path).query("delegation_events")
    }
    return served, stored


@pytest.mark.parametrize("correlation_id", [_PRICED, _PRICED_AGAIN])
def test_a_priced_call_is_served_with_the_cost_the_store_holds(
    tmp_path: Path, correlation_id: str
) -> None:
    served, stored = _served_and_stored(tmp_path)
    assert stored[correlation_id] == _COSTS[correlation_id]
    assert served[correlation_id]["cost_usd"] == stored[correlation_id]


def test_the_served_costs_of_the_day_add_up_to_the_stored_costs(
    tmp_path: Path,
) -> None:
    served, stored = _served_and_stored(tmp_path)
    assert set(served) == set(_COSTS)
    served_sum = sum(
        float(str(row["cost_usd"]))
        for row in served.values()
        if row["cost_usd"] is not None
    )
    stored_sum = sum(float(str(cost)) for cost in stored.values() if cost is not None)
    assert served_sum == pytest.approx(stored_sum, abs=1e-12)
    assert served_sum == pytest.approx(0.0123 + 0.000457, abs=1e-12)


def test_a_call_with_no_cost_is_left_out_of_the_sum_not_counted_as_zero(
    tmp_path: Path,
) -> None:
    served, _ = _served_and_stored(tmp_path)
    assert served[_NO_COST]["cost_usd"] is None
    assert served[_FREE]["cost_usd"] == 0.0
    priced = [row for row in served.values() if row["cost_usd"] is not None]
    assert len(priced) == 3


class _RecordingPublisher:
    """Captures every published snapshot delta instead of reaching a broker."""

    def __init__(self) -> None:
        self.messages: list[ModelSnapshotDeltaMessage] = []

    def publish(self, message: ModelSnapshotDeltaMessage) -> bool:
        self.messages.append(message)
        return True


_SLUG = "omn20717-tenant"
_TENANT_UUID = UUID("00000000-0000-4000-8000-000000020717")


def _published_and_stored(cost: float) -> tuple[dict[str, object], dict[str, object]]:
    """Drive the real terminal write path; return the republished row and the stored one."""
    publisher = _RecordingPublisher()
    db = InmemoryDatabaseAdapter()
    db.upsert(
        TENANT_REGISTRY_MIRROR_TABLE,
        "tenant_slug",
        {
            "tenant_slug": _SLUG,
            "tenant_uuid": _TENANT_UUID,
            "status": "active",
            "source_event_id": "c0000000-0000-0000-0000-000000020717",
        },
    )
    HandlerProjectionDelegation(publisher=publisher).project(
        ModelTaskDelegatedEvent(
            correlation_id=_PRICED,
            tenant_id=_SLUG,
            session_id="s1",
            task_type="document",
            delegated_to="provider-model",
            model_name="provider-model",
            delegated_by="test",
            quality_gate_passed=True,
            cost_usd=cost,
            timestamp="2026-10-12T09:00:00+00:00",
        ),
        db,
    )
    deltas = [m for m in publisher.messages if m.topic == _DECISIONS]
    assert len(deltas) == 1, [m.topic for m in publisher.messages]
    published = json.loads(deltas[0].value.decode())["row"]
    stored = db.query("delegation_events")[0]
    return published, stored


def test_the_republished_row_carries_the_cost_the_store_returned() -> None:
    published, stored = _published_and_stored(0.0123)
    assert "cost_usd" in published
    assert float(str(stored["cost_usd"])) == pytest.approx(0.0123, abs=1e-12)
    assert float(str(published["cost_usd"])) == float(str(stored["cost_usd"]))


def test_two_calls_with_different_costs_republish_different_costs() -> None:
    first, _ = _published_and_stored(0.0123)
    second, _ = _published_and_stored(0.000457)
    assert float(str(first["cost_usd"])) != float(str(second["cost_usd"]))
