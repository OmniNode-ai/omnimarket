# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-15800 AC5: cross-boundary seam test against REAL infrastructure.

``test_projection_bus_seam.py`` drives the same three seams with a mocked
``AsyncpgAdapter`` and a fake ``AIOKafkaProducer``. That is a fast, always-runnable
guard, but AC5 names the real components explicitly:

    publish a real event -> the real reducer under the real BaseProjectionRunner
    against real Postgres -> a real message on the snapshot topic -> the real
    FastAPI app with the real SnapshotCache consumer against the same broker.

This module is that variant. Nothing between the reducer and the HTTP response is
a double:

  Seam A input: the source event is published to the real broker with a real
  ``AIOKafkaProducer``, and the runner is driven through its own
  ``BaseProjectionRunner.run()`` entrypoint -- it builds its own real
  ``AIOKafkaConsumer``, subscribes to its contract topics, unwraps the envelope
  and dispatches to ``project_event`` itself. The test never calls
  ``project_event`` directly, so the only way a row can appear is if a real
  message crossed a real broker and the real consumer picked it up.

  Seam A output: the runner writes through its own real ``AsyncpgAdapter`` pool
  into real Postgres (the row is then read back on an independent connection, so
  the assertion does not trust the reducer's own RETURNING output), and publishes
  the snapshot delta through the real ``AIOKafkaProducer`` that
  ``_ensure_producer()`` builds from the runtime binding.

  Seam B: a real ``SnapshotCache`` consumer joins the same broker and applies the
  message the reducer actually published -- no hand-fed bytes.

  Seam C: the real FastAPI app serves ``GET /projection/{topic}`` from that cache.

Skips (never silent false-greens, see scripts/ci/integration_skip_guard.yaml):
Postgres absence reuses the shared fixtures' reasons; broker absence reports
``Kafka not reachable``, which the guard classifies as an expected optional skip
because the merge-gating job runs ``-m "not kafka"`` and provisions no broker.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Generator
from contextlib import contextmanager, suppress
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
from aiokafka import AIOKafkaProducer
from fastapi.testclient import TestClient

from omnimarket.nodes.node_projection_registration.handlers.handler_registration import (
    RegistrationProjectionRunner,
)
from omnimarket.projection.runner import PROJECTION_RUNTIME_BINDING_OVERLAY_ENV
from omnimarket.projection.snapshot_cache import SnapshotCache
from scripts.projection_api_server import app, get_snapshot_cache, get_topic_map

REGISTRATION_TOPIC = "onex.snapshot.projection.registration.v1"
INTROSPECTION_TOPIC = "onex.evt.platform.node-introspection.v1"

# Canonical DDL owner is omnimarket's own migration
# node_projection_registration/0000_create_node_service_registry.sql (OMN-11012).
# Applied here IF NOT EXISTS so the test runs against a bare integration database
# as well as one that already carries the migration.
_REGISTRY_DDL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS node_service_registry (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  service_name TEXT UNIQUE NOT NULL,
  service_url TEXT NOT NULL DEFAULT '',
  service_type TEXT,
  health_status TEXT DEFAULT 'unknown',
  last_health_check TIMESTAMPTZ,
  last_heartbeat_at TIMESTAMPTZ,
  uptime_seconds BIGINT DEFAULT 0,
  health_check_interval_seconds INT DEFAULT 60,
  metadata JSONB DEFAULT '{}'::jsonb,
  is_active BOOLEAN DEFAULT true,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  projected_at TIMESTAMPTZ DEFAULT NOW()
);
"""

# The cache must observe the reducer's message through a real group join,
# partition assignment and replay; on a cold broker that is seconds, not
# milliseconds. Bounded so a genuinely broken seam fails instead of hanging.
_CONSUMER_READY_TIMEOUT_SECONDS = 60.0
_PROJECTION_TIMEOUT_SECONDS = 90.0
_CACHE_READBACK_TIMEOUT_SECONDS = 60.0
_POLL_INTERVAL_SECONDS = 0.5


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


def _raise_if_runner_died(runner_task: asyncio.Task[None]) -> None:
    """Surface a runner crash immediately instead of waiting out a timeout."""
    if runner_task.done():
        exc = runner_task.exception()
        if exc is not None:
            raise AssertionError(
                f"BaseProjectionRunner.run() exited before the event was "
                f"projected: {exc!r}"
            ) from exc
        raise AssertionError(
            "BaseProjectionRunner.run() returned before the event was projected "
            "-- the consumer gave up (see MAX_RETRY_ATTEMPTS in runner.py)"
        )


async def _await_consumer_ready(
    runner: RegistrationProjectionRunner, runner_task: asyncio.Task[None]
) -> None:
    """Wait until run() has built and started its own consumer.

    Publishing before the group exists would still be replayed (the runner
    consumes with auto_offset_reset='earliest'), but waiting keeps the test
    honest about exercising the live path rather than recovery-by-replay.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _CONSUMER_READY_TIMEOUT_SECONDS
    while loop.time() < deadline:
        _raise_if_runner_died(runner_task)
        if runner._consumer is not None and runner._consumer.assignment():
            return
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    raise AssertionError(
        "BaseProjectionRunner.run() never reached a partition assignment within "
        f"{_CONSUMER_READY_TIMEOUT_SECONDS}s"
    )


async def _await_db_row(
    conn: asyncpg.Connection, service_name: str, runner_task: asyncio.Task[None]
) -> dict[str, Any]:
    """Poll real Postgres until the runner projects the consumed event."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _PROJECTION_TIMEOUT_SECONDS
    while loop.time() < deadline:
        _raise_if_runner_died(runner_task)
        row = await conn.fetchrow(
            "SELECT service_name, service_type, health_status, is_active "
            "FROM node_service_registry WHERE service_name = $1",
            service_name,
        )
        if row is not None:
            return dict(row)
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    raise AssertionError(
        f"{service_name!r} never reached node_service_registry within "
        f"{_PROJECTION_TIMEOUT_SECONDS}s -- the event was published but the real "
        "runner never consumed and projected it (Seam A is broken)"
    )


async def _await_cached_row(
    cache: SnapshotCache, topic: str, service_name: str
) -> dict[str, Any]:
    """Poll the real cache until the reducer's row arrives over the bus."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _CACHE_READBACK_TIMEOUT_SECONDS
    while loop.time() < deadline:
        for row in cache.get_rows(topic):
            if row.get("service_name") == service_name:
                return dict(row)
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    raise AssertionError(
        f"{service_name!r} never reached the SnapshotCache from {topic!r} within "
        f"{_CACHE_READBACK_TIMEOUT_SECONDS}s -- the reducer published but the real "
        "consumer never applied it (Seam B is broken)"
    )


@pytest.mark.integration
@pytest.mark.kafka
@pytest.mark.slow
class TestRegistrationEventToHttpReadbackRealInfra:
    @pytest.mark.asyncio
    async def test_registration_event_to_http_readback_real_infra(
        self,
        postgres_fixture: asyncpg.Connection,
        integration_postgres_dsn: str,
        integration_kafka_bootstrap: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        await postgres_fixture.execute(_REGISTRY_DDL)

        # Unique per run: the snapshot topic is compacted and shared, so every
        # assertion keys on this row rather than on the topic's total row count.
        run_id = uuid4().hex[:12]
        service_name = f"node-omn15800-realseam-{run_id}"

        # The runner resolves its own runtime binding from the environment --
        # the same resolution the deployed process uses. Nothing is injected.
        # An overlay path set in the ambient environment would take precedence
        # over these vars and silently point the runner at another broker/DB,
        # so it is cleared for the duration of this test.
        monkeypatch.delenv(
            PROJECTION_RUNTIME_BINDING_OVERLAY_ENV,
            raising=False,
        )
        monkeypatch.setenv("KAFKA_BROKERS", integration_kafka_bootstrap)
        monkeypatch.setenv("OMNIBASE_INFRA_DB_URL", integration_postgres_dsn)
        monkeypatch.setenv("KAFKA_CONSUMER_GROUP", f"omn15800-realseam-{run_id}")

        runner = RegistrationProjectionRunner()
        assert runner._runtime_binding is not None, (
            "RegistrationProjectionRunner must resolve a runtime binding from the "
            "environment for this test to exercise the real transport path"
        )
        assert (
            runner._runtime_binding.kafka_bootstrap_servers
            == integration_kafka_bootstrap
        )
        exposure = runner._snapshot_exposure
        assert exposure is not None
        assert exposure.topic == REGISTRATION_TOPIC
        assert exposure.key_columns == ("service_name",)

        cache = SnapshotCache(
            {REGISTRATION_TOPIC: exposure},
            bootstrap_servers=integration_kafka_bootstrap,
            group_id=f"omn15800-realseam-cache-{run_id}",
        )
        runner_task: asyncio.Task[None] | None = None

        try:
            # The snapshot-side consumer starts first so the delta the reducer
            # publishes is observed live rather than only through replay.
            await cache.start()

            # Seam A input leg: drive the runner through its OWN entrypoint, so
            # the real BaseProjectionRunner.run() builds the real
            # AIOKafkaConsumer, subscribes to its contract topics, unwraps the
            # envelope and dispatches to project_event itself. The test never
            # calls project_event directly -- the only way the row can appear is
            # if a real message crossed a real broker and the real consumer
            # picked it up.
            runner_task = asyncio.create_task(runner.run())
            await _await_consumer_ready(runner, runner_task)

            payload = {
                "node_name": service_name,
                "node_id": str(uuid4()),
                "node_type": "COMPUTE",
                "correlation_id": str(uuid4()),
                "service_url": "http://omn15800-realseam.invalid:9999",
            }
            producer = AIOKafkaProducer(bootstrap_servers=integration_kafka_bootstrap)
            await producer.start()
            try:
                await producer.send_and_wait(
                    INTROSPECTION_TOPIC, json.dumps(payload).encode("utf-8")
                )
            finally:
                await producer.stop()

            # Seam A output, independently verified: the row really is in
            # Postgres, read on a separate connection rather than trusting the
            # reducer's own RETURNING output. Reaching this state proves the
            # published event was consumed off the broker by the runner.
            db_row = await _await_db_row(postgres_fixture, service_name, runner_task)
            assert db_row["service_type"] == "COMPUTE"
            assert db_row["is_active"] is True

            # Seam B: the real consumer receives the real published message.
            cached_row = await _await_cached_row(
                cache, REGISTRATION_TOPIC, service_name
            )
            assert cached_row["service_type"] == db_row["service_type"]
            assert cached_row["health_status"] == db_row["health_status"]

            # Seam C: the real FastAPI app serves it, bus-backed, with no DB pool
            # anywhere in the request path.
            with _with_cache(cache, REGISTRATION_TOPIC, exposure) as client:
                resp = client.get(f"/projection/{REGISTRATION_TOPIC}")

            assert resp.status_code == 200
            body = resp.json()
            assert body["topic"] == REGISTRATION_TOPIC
            assert body["backing"] == "bus"
            served = next(
                (r for r in body["rows"] if r["service_name"] == service_name), None
            )
            assert served is not None, (
                "the row reached the cache but the HTTP envelope omitted it"
            )
            assert served["service_type"] == "COMPUTE"
            assert served["health_status"] == db_row["health_status"]
            assert served["is_active"] is True
        finally:
            # Tombstone the compacted topic so repeated runs do not accumulate
            # keys, then stop the runner and drop the row.
            with suppress(Exception):
                await runner.publish_snapshot_delta(
                    exposure,
                    op="delete",
                    row=None,
                    source_event_id=str(uuid4()),
                    source_topic=INTROSPECTION_TOPIC,
                    source_partition=0,
                    source_offset=1,
                    key={"service_name": service_name},
                )
            with suppress(Exception):
                await cache.stop()
            # shutdown() stops the consumer and producer and closes the DB pool
            # the runner opened for itself in run().
            with suppress(Exception):
                await runner.shutdown()
            if runner_task is not None:
                runner_task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await runner_task
            with suppress(Exception):
                await postgres_fixture.execute(
                    "DELETE FROM node_service_registry WHERE service_name = $1",
                    service_name,
                )
