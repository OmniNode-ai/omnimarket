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
  ``AIOKafkaProducer``, read back off it by a real ``AIOKafkaConsumer`` subscribed
  to the runner's OWN contract topics under the runner's OWN group id, and handed
  to the real ``BaseProjectionRunner._handle_message`` -- the same method
  ``run()`` calls for every message, so the envelope unwrap, the ``MessageMeta``
  built from broker-assigned coordinates, the ``project_event`` dispatch and the
  offset commit are all the production ones. The test never calls
  ``project_event`` directly, so the only way a row can appear is if a real
  message crossed a real broker.

  Out of scope by design: ``run()``'s process-lifecycle wrapper (POSIX signal
  handlers, health server, consumer-retry loop). ``run()`` installs signal
  handlers via ``loop.add_signal_handler``, which raises ``NotImplementedError``
  on Windows, so calling it here would make this test Linux-only for no gain in
  per-message coverage.

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
from aiokafka import AIOKafkaConsumer, AIOKafkaProducer
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


async def _consume_into_runner(
    runner: RegistrationProjectionRunner,
    *,
    bootstrap_servers: str,
    service_name: str,
) -> bool:
    """Read the runner's own topics off the real broker and hand each message to
    the real ``BaseProjectionRunner._handle_message``.

    Returns ``True`` once the message carrying ``service_name`` has been handled.
    The consumer is built from the runner's own contract topics and group id, so
    the subscription set is the one ``run()`` would use.
    """
    consumer = AIOKafkaConsumer(
        *runner.topics,
        bootstrap_servers=bootstrap_servers,
        group_id=runner._group_id,
        client_id=runner._client_id,
        auto_offset_reset="earliest",
        enable_auto_commit=False,
    )
    await consumer.start()
    try:
        runner._consumer = consumer
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _PROJECTION_TIMEOUT_SECONDS
        while loop.time() < deadline:
            batches = await consumer.getmany(timeout_ms=1000)
            for messages in batches.values():
                for message in messages:
                    # The real per-message path: envelope unwrap, MessageMeta
                    # from broker coordinates, project_event dispatch, commit.
                    await runner._handle_message(message)
                    if (
                        message.value is not None
                        and service_name.encode("utf-8") in message.value
                    ):
                        return True
        return False
    finally:
        runner._consumer = None
        with suppress(Exception):
            await consumer.stop()


async def _read_db_row(
    conn: asyncpg.Connection, service_name: str
) -> dict[str, Any] | None:
    """Read the projected row on a connection the runner does not own."""
    row = await conn.fetchrow(
        "SELECT service_name, service_type, health_status, is_active "
        "FROM node_service_registry WHERE service_name = $1",
        service_name,
    )
    return dict(row) if row is not None else None


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

        try:
            # The snapshot-side consumer starts first so the delta the reducer
            # publishes is observed live rather than only through replay.
            await cache.start()

            # The runner opens its own real pool against real Postgres; run()
            # would do this itself, and the consume path below needs it.
            await runner._db.connect()

            # Seam A input leg. The source event is published to the real broker
            # and read back off it by a real AIOKafkaConsumer subscribed to the
            # runner's OWN contract topics with the runner's OWN group id, then
            # handed to the real BaseProjectionRunner._handle_message -- the same
            # method run() calls for every message. That is the real envelope
            # unwrap, the real MessageMeta construction from broker-assigned
            # coordinates, the real project_event dispatch and the real offset
            # commit. The test never calls project_event itself.
            #
            # Why not runner.run() directly: run() installs POSIX signal handlers
            # via loop.add_signal_handler, which raises NotImplementedError on
            # Windows, so calling it would make this test Linux-only. Its
            # process-lifecycle wrapper (signal handling, health server, retry
            # loop) is deliberately out of scope here; everything it does per
            # message is exercised.
            assert INTROSPECTION_TOPIC in runner.topics, (
                "the runner's contract must declare the source topic for this "
                "test to consume the same subscription set run() would"
            )

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

            consumed = await _consume_into_runner(
                runner,
                bootstrap_servers=integration_kafka_bootstrap,
                service_name=service_name,
            )
            assert consumed is True, (
                "the published event was never delivered to the runner off the "
                "real broker"
            )

            # Seam A output, independently verified: the row really is in
            # Postgres, read on a separate connection rather than trusting the
            # reducer's own RETURNING output.
            db_row = await _read_db_row(postgres_fixture, service_name)
            assert db_row is not None, (
                "the runner consumed the event but no row exists in Postgres"
            )
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
            with suppress(Exception):
                await postgres_fixture.execute(
                    "DELETE FROM node_service_registry WHERE service_name = $1",
                    service_name,
                )
