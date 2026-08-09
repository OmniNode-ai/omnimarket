# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-boundary seam test for OMN-15800 (bus-read conversion slice).

2026-08-09 operator ruling (verbatim): "It should be accessing the
projections from the event bus not from a database. Nothing should be
connecting to a database other than the runtime."

Drives every real boundary the design names, crossing all three seams in one
test rather than three independent unit suites:

  Seam A (reducer -> snapshot message): the REAL
  ``RegistrationProjectionRunner.project_event`` (the deployed Kafka->Postgres
  reducer) processes a real ``ModelNodeIntrospectionEvent``-shaped payload,
  writes it (mocked DB, RETURNING-shaped response), and calls the REAL
  ``BaseProjectionRunner.publish_snapshot_delta`` -- captured at the producer
  boundary (a fake ``AIOKafkaProducer.send_and_wait``, not a hand-rolled
  publish_fn) so the exact key/headers/value bytes a live broker would receive
  are asserted.

  Seam B (message -> cache): those exact captured bytes are fed into the REAL
  ``SnapshotCache.apply_message`` -- the same method the live consumer loop
  calls, not a test-only stand-in.

  Seam C (cache -> HTTP): the REAL FastAPI app, with ``get_snapshot_cache``
  overridden to that cache and NO asyncpg pool anywhere, serves
  ``GET /projection/{topic}`` and the full envelope is asserted field-by-field
  against the row the reducer wrote.

No Kafka broker or Postgres instance is spun up: the AIOKafkaProducer/Consumer
transport boundary is a test double (this repo's established pattern --
test_omn12810_projection_publisher_injection.py drives the real runner class
the identical way), and the DB write is a mocked ``AsyncpgAdapter`` whose
``execute()`` returns the RETURNING-shaped row a real Postgres would return.
Every model/handler/cache/route class in between is the real production
class.

RED (documented — this test failed before OMN-15800 with an AttributeError:
``BaseProjectionRunner`` had no ``publish_snapshot_delta``, and
``omnimarket.projection.snapshot_cache`` did not exist).
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_registration.handlers.handler_registration import (
    RegistrationProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.snapshot_cache import SnapshotCache
from scripts.projection_api_server import app, get_snapshot_cache, get_topic_map

REGISTRATION_TOPIC = "onex.snapshot.projection.registration.v1"
INTROSPECTION_TOPIC = "onex.evt.platform.node-introspection.v1"


def _mock_db_returning(row: dict[str, Any]) -> Any:
    """A mocked AsyncpgAdapter whose execute() returns exactly what a real
    Postgres ``... RETURNING <declared columns>`` would: a one-row list."""
    mock_db = MagicMock(spec=AsyncpgAdapter)
    mock_db.execute = AsyncMock(return_value=[row])
    return mock_db


def _fake_producer() -> tuple[MagicMock, list[dict[str, Any]]]:
    """A fake AIOKafkaProducer capturing exactly the args a real broker call
    would receive -- topic/value/key/headers -- via send_and_wait."""
    sent: list[dict[str, Any]] = []

    async def _send_and_wait(
        topic: str,
        value: bytes | None = None,
        key: bytes | None = None,
        headers: list[tuple[str, bytes]] | None = None,
    ) -> None:
        sent.append({"topic": topic, "value": value, "key": key, "headers": headers})

    producer = MagicMock()
    producer.send_and_wait = AsyncMock(side_effect=_send_and_wait)
    return producer, sent


@contextmanager
def _with_cache(
    cache: SnapshotCache, topic: str, cfg: Any
) -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_snapshot_cache] = lambda: cache
    app.dependency_overrides[get_topic_map] = lambda: {topic: cfg}
    client = TestClient(app, raise_server_exceptions=True)
    try:
        yield client
    finally:
        app.dependency_overrides.clear()


@pytest.mark.unit
class TestRegistrationEventToHttpReadback:
    def test_registration_event_to_http_readback(self) -> None:
        # ------------------------------------------------------------------
        # Seam A: real reducer processes a real introspection payload, writes
        # (mocked DB, RETURNING-shaped), and publishes a real snapshot delta.
        # ------------------------------------------------------------------
        runner = RegistrationProjectionRunner()
        assert runner._snapshot_exposure is not None, (
            "RegistrationProjectionRunner must resolve a bus_backed exposure "
            "from its own contract.yaml"
        )
        exposure = runner._snapshot_exposure
        assert exposure.topic == REGISTRATION_TOPIC
        assert exposure.key_columns == ("service_name",)

        now = datetime.now(UTC)
        returned_row = {
            "service_name": "node-omn15800-seam",
            "service_type": "COMPUTE",
            "health_status": "healthy",
            "is_active": True,
            "last_health_check": now,
            "updated_at": now,
            "projected_at": now,
        }
        runner._db = _mock_db_returning(returned_row)
        fake_producer, sent = _fake_producer()
        runner._producer = fake_producer

        source_event_id = str(uuid4())
        payload = {
            "node_name": "node-omn15800-seam",
            "node_id": str(uuid4()),
            "node_type": "COMPUTE",
            "correlation_id": source_event_id,
            "service_url": "http://localhost:9999",
        }
        meta = MessageMeta(partition=0, offset=0, fallback_id="omn15800-fallback")

        ok = asyncio.run(runner.project_event(INTROSPECTION_TOPIC, payload, meta))
        assert ok is True

        # Exactly one snapshot delta was published, on the exposure's topic.
        assert len(sent) == 1
        published = sent[0]
        assert published["topic"] == REGISTRATION_TOPIC
        assert published["key"] == b"node-omn15800-seam"
        assert published["value"] is not None  # upsert, not a tombstone
        header_map = dict(published["headers"])
        assert header_map["schema_version"] == b"projection_snapshot.v1"
        assert header_map["content_type"] == b"application/json"

        # ------------------------------------------------------------------
        # Seam B: the exact captured bytes are applied to a REAL SnapshotCache
        # via the same apply_message() the live consumer loop calls.
        # ------------------------------------------------------------------
        topic_map = {REGISTRATION_TOPIC: exposure}
        cache = SnapshotCache(topic_map, bootstrap_servers="unused:9092")
        cache.apply_message(
            published["topic"],
            published["key"],
            published["value"],
            published["headers"],
        )
        # No live consumer in this test -- bootstrap is marked complete
        # directly, mirroring what a caught-up partition assignment does.
        cache._state[REGISTRATION_TOPIC].bootstrap_complete = True

        assert cache.row_count(REGISTRATION_TOPIC) == 1
        cached_rows = cache.get_rows(REGISTRATION_TOPIC)
        assert cached_rows[0]["service_name"] == "node-omn15800-seam"
        assert cached_rows[0]["health_status"] == "healthy"

        # ------------------------------------------------------------------
        # Seam C: the REAL FastAPI app serves the row over HTTP, with NO
        # asyncpg pool anywhere in the dependency graph.
        # ------------------------------------------------------------------
        with _with_cache(cache, REGISTRATION_TOPIC, exposure) as client:
            resp = client.get(f"/projection/{REGISTRATION_TOPIC}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["topic"] == REGISTRATION_TOPIC
        assert body["backing"] == "bus"
        assert body["row_count"] == 1
        row = body["rows"][0]
        assert row["service_name"] == "node-omn15800-seam"
        assert row["service_type"] == "COMPUTE"
        assert row["health_status"] == "healthy"
        assert row["is_active"] is True
        assert row["last_health_check"] == now.isoformat()
        assert row["updated_at"] == now.isoformat()
        assert row["projected_at"] == now.isoformat()


@pytest.mark.unit
def test_api_server_module_graph_reaches_no_asyncpg_or_psycopg2() -> None:
    """OMN-15800: the projection-api process holds zero DB driver.

    Asserts both statically (source text) and dynamically (the module's own
    imported names) that api_server.py never references asyncpg/psycopg2.
    """
    import inspect

    import omnimarket.projection.api_server as api_server_module

    source = inspect.getsource(api_server_module)
    assert "asyncpg" not in source
    assert "psycopg2" not in source

    module_vars = vars(api_server_module)
    assert "asyncpg" not in module_vars
    assert "get_pool" not in module_vars
    assert "_dsn" not in module_vars
    assert "ModelProjectionDatabaseBinding" not in module_vars
