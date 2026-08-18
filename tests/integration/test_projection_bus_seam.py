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
  reducer) processes real ``ModelNodeIntrospectionEvent``/heartbeat-shaped
  payloads, writes them through a REAL ``AsyncpgAdapter`` against an ephemeral,
  hermetic, freshly-migrated Postgres cluster (``EphemeralPostgres`` per
  ticket AC5 -- the established initdb/pg_ctl hermetic-cluster pattern already
  used by ``tests/test_cli_projection_writer_tenant_rls_omn15306.py`` and
  ``tests/test_roi_overlay_read_tenant_rls_omn16092.py``), and calls the REAL
  ``BaseProjectionRunner.publish_snapshot_delta`` -- which builds a REAL
  ``AIOKafkaProducer`` and publishes to a REAL broker.

  Seam B (message -> cache): a REAL ``SnapshotCache`` runs a REAL
  ``AIOKafkaConsumer`` against that SAME broker, replaying the compacted
  topic from earliest to end-of-partition (the live bootstrap path), not a
  hand-fed ``apply_message()`` call.

  Seam C (cache -> HTTP): the REAL FastAPI app, with ``get_snapshot_cache``
  overridden to that cache and NO asyncpg pool anywhere, serves
  ``GET /projection/{topic}`` and the full envelope is asserted field-by-field
  against the row read back directly from ``EphemeralPostgres``.

AC5 (2026-08-17 verification round) flagged that this test previously drove
the seam with a ``MagicMock`` ``AsyncpgAdapter`` and a hand-rolled fake
``AIOKafkaProducer`` -- it proved the call graph's SHAPE, never that the seam
actually crosses a real DB or a real broker. Every double below is now the
real thing; the two doubles' only remaining substitute is the ephemeral
cluster/broker's *location*, not their *reality*. Marked ``kafka`` (skips
without a reachable broker, this repo's established convention) and
``integration`` (skips without local ``initdb``/``pg_ctl``).

RED (documented — this test failed before OMN-15800 with an AttributeError:
``BaseProjectionRunner`` had no ``publish_snapshot_delta``, and
``omnimarket.projection.snapshot_cache`` did not exist).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import asyncpg
import pytest
from aiokafka import AIOKafkaProducer
from fastapi.testclient import TestClient

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_live_events.handlers.handler_live_events import (
    HandlerLiveEventsProjectionRunner,
)
from omnimarket.nodes.node_projection_registration.handlers.handler_registration import (
    RegistrationProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.snapshot_cache import SnapshotCache
from scripts.projection_api_server import app, get_snapshot_cache, get_topic_map

REGISTRATION_TOPIC = "onex.snapshot.projection.registration.v1"
INTROSPECTION_TOPIC = "onex.evt.platform.node-introspection.v1"
LIVE_EVENTS_TOPIC = "onex.snapshot.projection.live-events.v1"
NODE_HEARTBEAT_TOPIC = "onex.evt.platform.node-heartbeat.v1"

# Real dev-lane Redpanda -- the only real broker reachable from this repo's
# dev/CI hosts (no local broker is provisioned for this test's environment).
# Overridable so a host with its own local broker (or a future embedded
# fixture) does not have to reach across the LAN.
KAFKA_BOOTSTRAP_SERVERS = os.environ.get(
    "KAFKA_BOOTSTRAP_SERVERS",
    "192.168.86.201:19092",  # onex-allow-internal-ip OMN-15800 reason="dev-lane Redpanda; real broker for the AC5 real-seam proof, not a runtime default"
)


async def _real_broker_or_skip(bootstrap_servers: str) -> None:
    """Probe the broker before building any real client (established
    pattern: tests/integration/test_cost_event_publisher_kafka.py)."""
    probe = AIOKafkaProducer(bootstrap_servers=bootstrap_servers)
    try:
        await asyncio.wait_for(probe.start(), timeout=5)
        await probe.stop()
    except Exception as exc:
        pytest.skip(f"Kafka not reachable at {bootstrap_servers}: {exc}")


def _pg_bin(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for prefix in sorted(Path("/opt/homebrew/opt").glob("postgresql@*"), reverse=True):
        candidate = prefix / "bin" / name
        if candidate.exists():
            return str(candidate)
    return None


_INITDB = _pg_bin("initdb")
_PG_CTL = _pg_bin("pg_ctl")

_REGISTRATION_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_registration"
    / "migrations"
)
_REGISTRATION_MIGRATION_FILES = (
    "0000_create_node_service_registry.sql",
    "0001_add_heartbeat_columns.sql",
    "0002_node_service_registry_tenant_rls.sql",
    "0003_reconcile_heartbeat_observability.sql",
    "0004_node_service_registry_no_force_rls.sql",
)

# 0002 refuses to run without app_dashboard. Minimal, self-contained mirror
# of omnibase_infra forward migration 094_create_app_dashboard_role.sql
# (OMN-14899) -- the same inline copy
# tests/test_cli_projection_writer_tenant_rls_omn15306.py and
# tests/test_omn15909_real_postgres_projection_write_path_gate.py already
# carry, so this test needs no sibling-repo checkout.
_APP_DASHBOARD_ROLE_SQL = """
DO $$
BEGIN
  BEGIN
    CREATE ROLE app_dashboard WITH
      NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
  EXCEPTION
    WHEN duplicate_object OR unique_violation THEN
      NULL;
  END;
END;
$$;
"""


def _pg_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["LANG"] = "en_US.UTF-8"
    env["LC_ALL"] = "C"
    return env


@asynccontextmanager
async def _ephemeral_registration_postgres() -> AsyncIterator[AsyncpgAdapter]:
    """OMN-15800 AC5's ``EphemeralPostgres``: a hermetic, unix-socket-only,
    disposable Postgres cluster (established pattern --
    tests/test_cli_projection_writer_tenant_rls_omn15306.py,
    tests/test_roi_overlay_read_tenant_rls_omn16092.py), migrated with the
    REAL ``node_projection_registration`` migration files (0000-0004) in a
    throwaway database. Writes go through a real ``AsyncpgAdapter``/asyncpg
    pool the exact same way the deployed ``RegistrationProjectionRunner``
    does; the cluster is never shared with any other test or process.
    """
    if not _INITDB or not _PG_CTL:
        pytest.skip(
            "initdb/pg_ctl not available -- cannot bring up an ephemeral "
            "Postgres for the OMN-15800 AC5 real-seam proof"
        )

    root = Path(tempfile.mkdtemp(prefix="omn15800-ac5-pg-"))
    data_dir = root / "data"
    sock_dir = root / "sock"
    sock_dir.mkdir()
    dbname = "omn15800ac5"

    subprocess.run(
        [
            str(_INITDB),
            "-D",
            str(data_dir),
            "-U",
            "postgres",
            "--auth-local=trust",
            "--auth-host=trust",
            "-E",
            "UTF8",
        ],
        check=True,
        capture_output=True,
        env=_pg_subprocess_env(),
    )
    subprocess.run(
        [
            str(_PG_CTL),
            "-D",
            str(data_dir),
            "-l",
            str(root / "postgres.log"),
            "-o",
            f"-k {sock_dir} -h ''",
            "-w",
            "start",
        ],
        check=True,
        capture_output=True,
        env=_pg_subprocess_env(),
    )

    pool: asyncpg.Pool | None = None
    try:
        bootstrap = await asyncpg.connect(
            host=str(sock_dir), user="postgres", database="postgres"
        )
        try:
            await bootstrap.execute(f"CREATE DATABASE {dbname}")
        finally:
            await bootstrap.close()

        pool = await asyncpg.create_pool(
            host=str(sock_dir),
            user="postgres",
            database=dbname,
            min_size=1,
            max_size=3,
        )
        await pool.execute(_APP_DASHBOARD_ROLE_SQL)
        for name in _REGISTRATION_MIGRATION_FILES:
            await pool.execute((_REGISTRATION_MIGRATIONS_DIR / name).read_text())

        # Real pool, assigned directly (established pattern --
        # test_omn15909_real_postgres_projection_write_path_gate.py): the
        # unix-socket cluster needs no asyncpg-parseable DSN string since
        # every AsyncpgAdapter method reads self._pool, never self._dsn.
        adapter = AsyncpgAdapter(dsn="ephemeral-pool-assigned-directly")
        adapter._pool = pool
        yield adapter
    finally:
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.close()
        subprocess.run(
            [str(_PG_CTL), "-D", str(data_dir), "-m", "immediate", "-w", "stop"],
            check=False,
            capture_output=True,
            env=_pg_subprocess_env(),
        )
        shutil.rmtree(root, ignore_errors=True)


def _mock_db_returning(row: dict[str, Any]) -> Any:
    """A mocked AsyncpgAdapter whose execute() returns exactly what a real
    Postgres ``... RETURNING <declared columns>`` would: a one-row list.

    Still used by ``TestLiveEventsEventToHttpReadback`` below (out of
    OMN-15800 AC5's scope, which names only
    ``test_registration_event_to_http_readback``).
    """
    mock_db = MagicMock(spec=AsyncpgAdapter)
    mock_db.execute = AsyncMock(return_value=[row])
    return mock_db


def _fake_producer() -> tuple[MagicMock, list[dict[str, Any]]]:
    """A fake AIOKafkaProducer capturing exactly the args a real broker call
    would receive -- topic/value/key/headers -- via send_and_wait.

    Still used by ``TestLiveEventsEventToHttpReadback`` below (out of
    OMN-15800 AC5's scope, which names only
    ``test_registration_event_to_http_readback``); this repo's established
    pattern (test_omn12810_projection_publisher_injection.py) for driving the
    real runner class without a broker.
    """
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


@pytest.mark.integration
@pytest.mark.kafka
class TestRegistrationEventToHttpReadback:
    """OMN-15800 AC5: every double is real (see module docstring)."""

    async def test_registration_event_to_http_readback(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _real_broker_or_skip(KAFKA_BOOTSTRAP_SERVERS)
        # BaseProjectionRunner.kafka_bootstrap_servers reads this env var
        # (KAFKA_BROKERS_ENV) at call time -- setting it here, before
        # constructing the runner, makes _ensure_producer() build a REAL
        # AIOKafkaProducer against the SAME broker SnapshotCache below
        # consumes from, with zero test-side producer double.
        monkeypatch.setenv("KAFKA_BROKERS", KAFKA_BOOTSTRAP_SERVERS)

        async with _ephemeral_registration_postgres() as adapter:
            # ------------------------------------------------------------
            # Seam A: real reducer, real EphemeralPostgres write, real
            # AIOKafkaProducer publish.
            # ------------------------------------------------------------
            runner = RegistrationProjectionRunner()
            assert runner._snapshot_exposure is not None, (
                "RegistrationProjectionRunner must resolve a bus_backed "
                "exposure from its own contract.yaml"
            )
            exposure = runner._snapshot_exposure
            assert exposure.topic == REGISTRATION_TOPIC
            assert exposure.key_columns == ("service_name",)
            runner._db = adapter  # real AsyncpgAdapter, EphemeralPostgres

            service_name = f"node-omn15800-ac5-{uuid4().hex[:10]}"
            node_id = str(uuid4())

            # Seed the row via a real introspection event -- mirrors the
            # live sequence (a node registers via introspection before its
            # periodic heartbeats start updating it). This write is fixture
            # setup, not the seam AC5 names.
            introspection_payload = {
                "node_name": service_name,
                "node_id": node_id,
                "node_type": "COMPUTE",
                "correlation_id": str(uuid4()),
                "service_url": "http://localhost:9999",
            }
            introspection_meta = MessageMeta(
                partition=0, offset=0, fallback_id=f"{service_name}-introspect"
            )
            seeded = await runner.project_event(
                INTROSPECTION_TOPIC, introspection_payload, introspection_meta
            )
            assert seeded is True

            # The seam AC5 names: a real onex.evt.platform.node-heartbeat.v1
            # envelope.
            heartbeat_payload = {
                "service_name": service_name,
                "node_id": node_id,
                "health_status": "healthy",
                "uptime_seconds": 42,
            }
            heartbeat_meta = MessageMeta(
                partition=0, offset=1, fallback_id=f"{service_name}-heartbeat"
            )
            ok = await runner.project_event(
                NODE_HEARTBEAT_TOPIC, heartbeat_payload, heartbeat_meta
            )
            assert ok is True

            # Ground truth: the actual row EphemeralPostgres now holds,
            # read back directly -- what the HTTP envelope below is
            # compared against field-by-field.
            db_rows = await adapter.execute(
                "SELECT service_name, service_type, health_status, is_active, "
                "last_health_check, updated_at, projected_at "
                "FROM node_service_registry WHERE service_name = $1",
                service_name,
            )
            assert len(db_rows) == 1, (
                f"expected exactly one EphemeralPostgres row for "
                f"{service_name!r}, found {len(db_rows)}"
            )
            db_row = db_rows[0]
            assert db_row["health_status"] == "healthy"
            assert db_row["is_active"] is True

            # ------------------------------------------------------------
            # Seam B: a REAL SnapshotCache with a REAL AIOKafkaConsumer,
            # against the SAME broker -- bootstraps by replaying the
            # compacted topic from earliest to end-of-partition.
            # ------------------------------------------------------------
            topic_map = {REGISTRATION_TOPIC: exposure}
            cache = SnapshotCache(
                topic_map,
                bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
                # Explicit override (OMN-15840): this test exercises the bus
                # seam, not the default group-id derivation, which requires
                # ONEX_ENVIRONMENT.
                group_id=f"omn15800-ac5-seam-{uuid4().hex[:8]}",
            )
            await cache.start()
            try:
                # The shared dev-lane topic can carry a real, several-
                # thousand-message backlog (observed: 3582 on
                # registration.v1) -- SnapshotCache's own internal poll
                # window (40 attempts x 0.5s = 20s) only checks partition
                # assignment/offset position, and the actual catch-up
                # consumption happens afterward in its batched main loop
                # (max 500 records/batch). 120s gives that comfortable
                # headroom without hard-coding a bespoke short timeout that
                # would flake as the shared topic grows.
                for _attempt in range(240):
                    if cache.is_bootstrapped(REGISTRATION_TOPIC):
                        break
                    await asyncio.sleep(0.5)
                else:
                    pytest.fail(
                        "SnapshotCache did not bootstrap "
                        f"{REGISTRATION_TOPIC} against the real broker "
                        "within 120s"
                    )

                # The real topic may carry other real/live-lane rows
                # (compacted, keyed by service_name) -- match on our own
                # unique key rather than asserting a total row count.
                cached_rows = cache.get_rows(REGISTRATION_TOPIC, limit=10_000)
                cache_matches = [
                    r for r in cached_rows if r["service_name"] == service_name
                ]
                assert len(cache_matches) == 1, (
                    f"expected exactly one cached row for {service_name!r} "
                    f"after a real broker round trip, found "
                    f"{len(cache_matches)} of {len(cached_rows)} cached rows"
                )
                assert cache_matches[0]["health_status"] == "healthy"

                # --------------------------------------------------------
                # Seam C: the REAL FastAPI app serves the row over HTTP,
                # with NO asyncpg pool anywhere in the dependency graph --
                # asserted field-by-field against the EphemeralPostgres
                # ground truth read back above.
                # --------------------------------------------------------
                with _with_cache(cache, REGISTRATION_TOPIC, exposure) as client:
                    resp = client.get(f"/projection/{REGISTRATION_TOPIC}")

                assert resp.status_code == 200
                body = resp.json()
                assert body["topic"] == REGISTRATION_TOPIC
                assert body["backing"] == "bus"
                http_matches = [
                    r for r in body["rows"] if r["service_name"] == service_name
                ]
                assert len(http_matches) == 1, (
                    f"expected exactly one HTTP row for {service_name!r}, "
                    f"found {len(http_matches)} of {body['row_count']} rows"
                )
                row = http_matches[0]
                assert row["service_name"] == db_row["service_name"]
                assert row["service_type"] == db_row["service_type"]
                assert row["health_status"] == db_row["health_status"]
                assert row["is_active"] == db_row["is_active"]
                assert (
                    row["last_health_check"] == db_row["last_health_check"].isoformat()
                )
                assert row["updated_at"] == db_row["updated_at"].isoformat()
                assert row["projected_at"] == db_row["projected_at"].isoformat()
            finally:
                await cache.stop()


@pytest.mark.unit
class TestLiveEventsEventToHttpReadback:
    """OMN-15800 follow-up: closes the system_event_stream not_yet_bus_backed
    gap the first business-proof gate run surfaced (deploy run 31431199686:
    ``FAIL system_event_stream: ... HTTP 503 (expected 200)``,
    ``error=not_yet_bus_backed``). Same three-seam shape as
    ``TestRegistrationEventToHttpReadback`` above, driving
    ``HandlerLiveEventsProjectionRunner`` instead of
    ``RegistrationProjectionRunner``.
    """

    def test_live_events_event_to_http_readback(self) -> None:
        # ------------------------------------------------------------------
        # Seam A: real reducer processes a real node-heartbeat payload,
        # writes (mocked DB, RETURNING-shaped), and publishes a real
        # snapshot delta.
        # ------------------------------------------------------------------
        runner = HandlerLiveEventsProjectionRunner()
        assert runner._snapshot_exposure is not None, (
            "HandlerLiveEventsProjectionRunner must resolve a bus_backed "
            "exposure from its own contract.yaml"
        )
        exposure = runner._snapshot_exposure
        assert exposure.topic == LIVE_EVENTS_TOPIC
        assert exposure.key_columns == ("event_id",)

        now = datetime.now(UTC)
        event_id = "omn15800-live-events-seam"
        returned_row = {
            "id": str(uuid4()),
            "event_id": event_id,
            "type": "ACTION",
            "timestamp": now,
            "source": "node-omn15800-seam",
            "topic": NODE_HEARTBEAT_TOPIC,
            "summary": "heartbeat ok",
            "payload": '{"node_name": "node-omn15800-seam"}',
            "correlation_id": None,
            "created_at": now,
        }
        runner._db = _mock_db_returning(returned_row)
        fake_producer, sent = _fake_producer()
        runner._producer = fake_producer

        payload = {
            "event_id": event_id,
            "node_name": "node-omn15800-seam",
            "summary": "heartbeat ok",
        }
        meta = MessageMeta(partition=0, offset=0, fallback_id="omn15800-le-fallback")

        ok = asyncio.run(runner.project_event(NODE_HEARTBEAT_TOPIC, payload, meta))
        assert ok is True

        # Exactly one snapshot delta was published, on the exposure's topic.
        assert len(sent) == 1
        published = sent[0]
        assert published["topic"] == LIVE_EVENTS_TOPIC
        assert published["key"] == event_id.encode("utf-8")
        assert published["value"] is not None  # upsert, not a tombstone
        header_map = dict(published["headers"])
        assert header_map["schema_version"] == b"projection_snapshot.v1"
        assert header_map["content_type"] == b"application/json"

        # ------------------------------------------------------------------
        # Seam B: the exact captured bytes are applied to a REAL
        # SnapshotCache via the same apply_message() the live consumer loop
        # calls.
        # ------------------------------------------------------------------
        topic_map = {LIVE_EVENTS_TOPIC: exposure}
        cache = SnapshotCache(
            topic_map,
            bootstrap_servers="unused:9092",
            group_id="test-bus-seam-group-live-events",
        )
        cache.apply_message(
            published["topic"],
            published["key"],
            published["value"],
            published["headers"],
        )
        cache._state[LIVE_EVENTS_TOPIC].bootstrap_complete = True

        assert cache.row_count(LIVE_EVENTS_TOPIC) == 1
        cached_rows = cache.get_rows(LIVE_EVENTS_TOPIC)
        assert cached_rows[0]["event_id"] == event_id
        assert cached_rows[0]["source"] == "node-omn15800-seam"

        # ------------------------------------------------------------------
        # Seam C: the REAL FastAPI app serves the row over HTTP, with NO
        # asyncpg pool anywhere in the dependency graph.
        # ------------------------------------------------------------------
        with _with_cache(cache, LIVE_EVENTS_TOPIC, exposure) as client:
            resp = client.get(f"/projection/{LIVE_EVENTS_TOPIC}")

        assert resp.status_code == 200
        body = resp.json()
        assert body["topic"] == LIVE_EVENTS_TOPIC
        assert body["backing"] == "bus"
        assert body["row_count"] == 1
        row = body["rows"][0]
        assert row["event_id"] == event_id
        assert row["type"] == "ACTION"
        assert row["source"] == "node-omn15800-seam"
        assert row["topic"] == NODE_HEARTBEAT_TOPIC
        assert row["summary"] == "heartbeat ok"
        assert row["timestamp"] == now.isoformat()
        assert row["created_at"] == now.isoformat()


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


@pytest.mark.unit
def test_api_server_import_never_loads_asyncpg_or_psycopg2_in_sys_modules() -> None:
    """OMN-15800 AC6: importing api_server must not pull asyncpg/psycopg2 in.

    The source/namespace check above only inspects ``api_server.py``'s own
    text and top-level names -- it cannot see a transitive eager import
    pulled in by a name api_server.py imports from elsewhere (e.g.
    ``omnimarket.projection.runner`` importing ``AsyncpgAdapter`` at module
    scope). It is also blind in-process: once any earlier test in the same
    pytest session imports asyncpg for any reason, ``sys.modules`` already
    has it before this test runs, so even a ``sys.modules`` assertion taken
    in-process would be contaminated.

    This test runs in a fresh subprocess that imports ONLY
    ``omnimarket.projection.api_server`` and reports whether asyncpg/
    psycopg2 ended up in ``sys.modules`` -- the only way to observe the
    real, isolated import graph the deployed projection-api process has.
    """
    probe = (
        "import sys\n"
        "import omnimarket.projection.api_server\n"
        "print('asyncpg' in sys.modules)\n"
        "print('psycopg2' in sys.modules)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    has_asyncpg, has_psycopg2 = completed.stdout.strip().splitlines()
    assert has_asyncpg == "False", (
        "asyncpg landed in sys.modules via the api_server import graph "
        f"(subprocess stdout: {completed.stdout!r})"
    )
    assert has_psycopg2 == "False", (
        "psycopg2 landed in sys.modules via the api_server import graph "
        f"(subprocess stdout: {completed.stdout!r})"
    )
