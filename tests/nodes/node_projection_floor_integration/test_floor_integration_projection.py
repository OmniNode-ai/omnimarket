# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for the floor-integration projection."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.floor_projection import ModelFloorProjectionEnvelope
from omnimarket.nodes.node_integration_projection_compute.handlers import (
    HandlerIntegrationProjection,
)
from omnimarket.nodes.node_projection_floor_integration.handlers.handler_floor_integration_writer import (
    FloorIntegrationProjectionWriter,
)
from omnimarket.nodes.node_projection_floor_integration.ports.floor_integration_store import (
    SqliteFloorIntegrationStore,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.models import (
    ModelProjectionReadRequest,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.models import ProjectionTableConfig
from tests.nodes.node_integration_reading_publish_effect.test_integration_reading_publish import (
    _batch,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_projection_floor_integration/contract.yaml"
)
EVENT = "onex.evt.omnimarket.floor-integration-pass.v1"
TOPIC = "onex.snapshot.projection.floor-integration-rows.v1"

Payload = dict[str, Any]
Rows = dict[str, dict[str, Any]]
Served = tuple[FloorIntegrationProjectionWriter, HandlerProjectionRead]


def _keys(projection: Any) -> set[str]:
    """The projection's row keys (its rows are a dict keyed by repository#number)."""
    return set(projection.rows)


def _normalize_as_of(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        value = value.isoformat()
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return text


def _project(batch: Any) -> tuple[Any, Payload]:
    projection = HandlerIntegrationProjection().handle(batch)
    envelope = ModelFloorProjectionEnvelope(
        projection_id="integration",
        as_of=projection.as_of,
        producer="test",
        payload=projection.model_dump(mode="json"),
    ).model_dump(mode="json")
    return projection, envelope


def _feed(
    writer: FloorIntegrationProjectionWriter, items: list[tuple[str, Payload]]
) -> None:
    for index, (topic, payload) in enumerate(items):
        writer.handle({**payload, "_topic": topic, "_partition": 0, "_offset": index})


def _topic_map() -> dict[str, ProjectionTableConfig]:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    return {
        exposure.topic: exposure
        for exposure in load_projection_exposures_from_contract(
            contract, "projection_floor_integration", CONTRACT
        )
    }


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Served:
    db = tmp_path / "floor.sqlite"
    writer = FloorIntegrationProjectionWriter(store=SqliteFloorIntegrationStore(db))
    overlay = tmp_path / "projection_binding.yaml"
    overlay.write_text(
        f"kafka_bootstrap_servers: inmemory\ndatabase_url: 'sqlite:///{db}'\n"
    )
    monkeypatch.setenv("OMNIMARKET_PROJECTION_RUNTIME_BINDING_OVERLAY", str(overlay))
    return writer, HandlerProjectionRead(topic_map=_topic_map())


async def _read(reader: HandlerProjectionRead) -> Rows:
    result = await reader.handle(ModelProjectionReadRequest(topic=TOPIC))
    assert result.ok, result
    return {str(row["key"]): row for row in result.rows}


def _without_projected(rows: Rows) -> Rows:
    return {
        key: {field: value for field, value in row.items() if field != "projected_at"}
        for key, row in rows.items()
    }


def test_floor_integration_exposure_declared() -> None:
    exposures = _topic_map()
    assert len(exposures) == 1
    exposure = exposures[TOPIC]
    assert exposure.topic == TOPIC
    assert exposure.bus_backed is True
    assert exposure.key_columns == ("key",)
    assert set(exposure.columns) == {
        "key",
        "repository",
        "number",
        "title",
        "merged_at",
        "target",
        "state",
        "gap_class",
        "step",
        "running_path",
        "running_line",
        "merged",
        "seen_running",
        "settled_elsewhere",
        "reading_owed",
        "unintegrated",
        "as_of",
        "projected_at",
    }


async def test_floor_integration_exposure_served(served: Served) -> None:
    writer, reader = served
    batch = _batch()
    projection, envelope = _project(batch)
    _feed(writer, [(EVENT, envelope)])

    rows = await _read(reader)

    assert set(rows) == _keys(projection)
    for row in rows.values():
        assert row["seen_running"] == projection.counts.seen_running
    assert (
        sum(1 for row in rows.values() if row["state"] == "running")
        == projection.counts.seen_running
    )

    exposure = _topic_map()[TOPIC]
    for row in rows.values():
        assert set(row) == set(exposure.columns)

    await reader.close()


async def test_floor_integration_newer_pass_drops_absent_key(served: Served) -> None:
    writer, reader = served
    batch = _batch()
    readings = batch.readings
    later = batch.as_of + timedelta(hours=1)
    newer_batch = _batch().model_copy(update={"as_of": later, "readings": readings[1:]})

    first_projection, first_envelope = _project(batch)
    newer_projection, newer_envelope = _project(newer_batch)

    _feed(writer, [(EVENT, first_envelope)])
    _feed(writer, [(EVENT, newer_envelope)])

    rows = await _read(reader)
    first_keys = _keys(first_projection)
    newer_keys = _keys(newer_projection)

    assert first_keys - newer_keys
    for dropped_key in first_keys - newer_keys:
        assert dropped_key not in rows

    for row in rows.values():
        assert _normalize_as_of(row["as_of"]) == _normalize_as_of(
            newer_projection.as_of
        )

    await reader.close()


async def test_floor_integration_older_pass_ignored(served: Served) -> None:
    writer, reader = served
    batch = _batch()
    readings = batch.readings
    later = batch.as_of + timedelta(hours=1)
    newer_batch = _batch().model_copy(update={"as_of": later, "readings": readings[1:]})

    _, older_envelope = _project(batch)
    _, newer_envelope = _project(newer_batch)

    _feed(writer, [(EVENT, newer_envelope)])
    newer_rows = await _read(reader)

    _feed(writer, [(EVENT, older_envelope)])
    rows = await _read(reader)

    assert rows == newer_rows
    await reader.close()


async def test_floor_integration_replay_same_result(served: Served) -> None:
    writer, reader = served
    batch = _batch()
    _, envelope = _project(batch)

    _feed(writer, [(EVENT, envelope)])
    first = await _read(reader)

    _feed(writer, [(EVENT, envelope)])
    second = await _read(reader)

    assert _without_projected(first) == _without_projected(second)
    await reader.close()
