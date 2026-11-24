# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain tests for node_projection_floor_integration.

Catches: the publisher's event topic and the writer's subscription drifting
apart; declared topic hops and ACL grants drifting; a published pass not
reaching the floor_integration_rows table; and a malformed pass writing rows.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.integration_reading import ModelIntegrationPublishCommand
from omnimarket.nodes.node_floor_owner_map_publish_effect.handlers import (
    InMemoryEnvelopePublisher,
)
from omnimarket.nodes.node_integration_projection_compute.handlers import (
    HandlerIntegrationProjection,
)
from omnimarket.nodes.node_integration_reading_publish_effect.handlers import (
    HandlerIntegrationReadingPublish,
)
from omnimarket.nodes.node_projection_floor_integration.handlers.handler_floor_integration_writer import (
    FloorIntegrationProjectionWriter,
)
from omnimarket.nodes.node_projection_floor_integration.ports.floor_integration_store import (
    SqliteFloorIntegrationStore,
)
from tests.nodes.node_integration_reading_publish_effect.test_integration_reading_publish import (
    _batch,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
WRITER_CONTRACT = NODES / "node_projection_floor_integration/contract.yaml"
PUBLISHER_CONTRACT = NODES / "node_integration_reading_publish_effect/contract.yaml"

PASS_EVENT = "onex.evt.omnimarket.floor-integration-pass.v1"  # onex-topic-allow: declared subscribe topic
SNAPSHOT_TOPIC = "onex.snapshot.projection.floor-integration-rows.v1"  # onex-topic-allow: declared publish topic
MALFORMED_DLQ = "onex.dlq.omnimarket.projection-floor-integration-malformed.v1"  # onex-topic-allow: declared dlq topic


def _producer_payload() -> tuple[Any, dict[str, Any]]:
    """The envelope the integration publisher itself puts on the pass event topic."""
    projection = HandlerIntegrationProjection().handle(_batch())
    snapshot, event = InMemoryEnvelopePublisher(), InMemoryEnvelopePublisher()
    handler = HandlerIntegrationReadingPublish(
        publisher=snapshot, producer="golden-chain", event_publisher=event
    )
    asyncio.run(
        handler.handle(
            ModelIntegrationPublishCommand(
                projection_id="integration", projection=projection
            )
        )
    )
    (envelope,) = event.published
    return projection, envelope.model_dump(mode="json")


def test_golden_chain_hops_are_the_declared_topics() -> None:
    writer = yaml.safe_load(WRITER_CONTRACT.read_text())
    publisher = yaml.safe_load(PUBLISHER_CONTRACT.read_text())

    assert PASS_EVENT in publisher["event_bus"]["publish_topics"]
    assert publisher["runtime"]["event_topic"] == PASS_EVENT
    assert writer["event_bus"]["subscribe_topics"] == [PASS_EVENT]
    assert writer["event_bus"]["publish_topics"] == [SNAPSHOT_TOPIC]
    assert writer["event_bus"]["dlq_topics"] == [MALFORMED_DLQ]

    exposure = writer["projection_api"]["exposures"][0]
    assert exposure["topic"] == SNAPSHOT_TOPIC
    assert exposure["bus_backed"] is True

    names = [
        entry["handler"]["name"] for entry in writer["handler_routing"]["handlers"]
    ]
    assert names == ["FloorIntegrationProjectionWriter"]
    assert FloorIntegrationProjectionWriter.onex_runtime_inprocess_dispatch is True

    def operations_for(contract: dict[str, Any], topic: str) -> list[str]:
        return [
            grant["operation"]
            for principal in contract.get("broker_acl_grants", [])
            for grant in principal.get("grants", [])
            if grant.get("name") == topic
        ]

    assert "WRITE" in operations_for(publisher, PASS_EVENT)
    assert "READ" in operations_for(writer, PASS_EVENT)
    assert "WRITE" in operations_for(writer, SNAPSHOT_TOPIC)


def test_golden_chain_published_pass_reaches_the_table(tmp_path: Path) -> None:
    db_path = tmp_path / "floor.db"
    writer = FloorIntegrationProjectionWriter(
        store=SqliteFloorIntegrationStore(db_path)
    )
    projection, payload = _producer_payload()

    result = writer.handle(
        {**payload, "_topic": PASS_EVENT, "_partition": 0, "_offset": 1}
    )

    assert result["rows_upserted"] == len(projection.rows)
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT key, state, seen_running FROM floor_integration_rows"
        ).fetchall()
    assert {key for key, _state, _seen in rows} == set(projection.rows)
    assert sum(1 for _key, state, _seen in rows if state == "running") == (
        projection.counts.seen_running
    )
    assert {seen for _key, _state, seen in rows} == {projection.counts.seen_running}


def test_golden_chain_error_leg_malformed_pass_writes_nothing(tmp_path: Path) -> None:
    db_path = tmp_path / "floor.db"
    writer = FloorIntegrationProjectionWriter(
        store=SqliteFloorIntegrationStore(db_path)
    )
    _projection, payload = _producer_payload()
    # The producer's own envelope with its pass garbled in transit.
    garbled = payload | {"payload": {"rows": "not a projection"}}

    with pytest.raises(ValidationError):
        writer.handle({**garbled, "_topic": PASS_EVENT, "_partition": 0, "_offset": 1})

    with sqlite3.connect(db_path) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'floor_integration_rows'"
        ).fetchall()
        count = (
            connection.execute(
                "SELECT COUNT(*) FROM floor_integration_rows"
            ).fetchone()[0]
            if tables
            else 0
        )
    assert count == 0
