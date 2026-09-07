# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16831 LAB PROOF: a real broker + a real database produce the real row.

The sibling unit file
(``tests/test_omn16831_delegation_writer_zero_rows.py``) drives
``project_event`` over payloads the shipped ``unwrap_envelope`` produced, against
an adapter double. That proves the two rejections and their repair, but every
byte still stayed inside one Python process: nothing there proves an INSERT is
accepted by a ``uuid`` column under a live RLS policy, and nothing there proves
the payload survives a real serialize/produce/fetch round trip.

This file closes both. It runs the DEPLOYED async path --
``DelegationProjectionRunner._handle_message``, the exact method the
``omnimarket-projection-delegation-writer`` container runs -- over
``aiokafka.ConsumerRecord`` objects a REAL ``AIOKafkaConsumer`` fetched from a
REAL broker, after a REAL ``AIOKafkaProducer`` published them, writing through
the REAL ``AsyncpgAdapter`` into a REAL PostgreSQL database. The two events are
the two the onex-dev delegations actually emitted, on their real contract topic
names, carrying one fixed correlation id.

WHAT IS ISOLATED AND WHY. The events go to the lane's real topics, so the
messages are indistinguishable from live traffic. The WRITE target is a
disposable schema this file creates and drops, so the lane's own
``delegation_events`` is never touched. Isolating the write, not the publish, is
deliberate: the publish is the half that must be real for the proof to mean
anything, and the write is the half that must be isolated for the lane to be
safe.

SKIPS, never ERRORs, without a reachable broker and database -- the harness
pattern of ``tests/test_omn16804_registry_resolved_write_tenant_real_postgres.py``,
whose migration-apply sequence and disposable-schema fixture this reuses.

RUN IT AS THE CONSTRAINED ROLE. ``INTEGRATION_POSTGRES_USER`` should be the
lane's own non-superuser projection login, not a superuser: a superuser carries
BYPASSRLS, and a green run under BYPASSRLS would prove the column accepts the
value while saying nothing about whether the ``tenant_isolation`` policy admits
the write. That policy compares a ``uuid`` column against
``current_setting('app.tenant_id', true)``, and the writer sets that GUC from
the row it is about to store -- so under a constrained role this test proves the
whole chain, RLS included. The role needs CREATE on the target database, which
is why the target should be a disposable database rather than the lane's own.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from uuid import UUID

import asyncpg
import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.projection.tenant_registry_resolution import (
    TENANT_REGISTRY_MIRROR_TABLE,
)

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "migrations"
)
# The migrations this harness deliberately does NOT apply, so the schema it
# builds is the schema onex-dev actually has. Read live off the onex-dev
# delegation writer on 2026-09-07 (SSM read-only, dev-system cluster
# i-06169517a92b45f86, comment omn16831-zero-rows):
#
#   delegation_events.tenant_id : data_type=text  default='omninode'::text
#   relrowsecurity=true  relforcerowsecurity=true  owner=role_omninode_owner
#   policy tenant_isolation USING/CHECK: tenant_id = current_setting('app.tenant_id', true)
#   connected as role_omnidash: rolsuper=false, rolbypassrls=false
#
# i.e. the OMN-15683/OMN-16930 UUID conversion (0031 fenced, 0032/0033/0034)
# has NOT been applied there -- a migration-state fact, established by reading
# the live catalog rather than by reading the migration directory. Applying the
# full set here would build a schema the deployment does not have, and a proof
# against the wrong schema is not a proof. ``test_the_harness_reproduces_the_
# onex_dev_shape`` below asserts every one of those values, so this harness
# cannot silently drift away from the lane it claims to reproduce.
_UNAPPLIED_ON_ONEX_DEV = (
    "0031_delegation_events_tenant_id_to_uuid.sql",
    "0032_delegation_events_tenant_id_uuid_via_registry.sql",
    "0033_delegation_events_uuid_via_registry_single_transaction.sql",
    "0034_delegation_events_uuid_via_registry_role_set_guard.sql",
)

# Fixed, so the readback quoted in the PR body is reproducible by anyone.
LAB_CORRELATION_ID = "16831ab0-0000-4000-8000-00000016831c"
LAB_TENANT_SLUG = "omn16831-lab-tenant"
LAB_TENANT_UUID = UUID("16831b00-0000-4000-8000-0000168310ab")

# The RLS migrations refuse to apply without the constrained read role
# (OMN-14899). Existence-checked FIRST, because unlike the sibling harness this
# file is expected to run as a NON-superuser, NON-CREATEROLE login -- that is
# the point, see the module docstring -- and ``CREATE ROLE`` would then raise
# ``insufficient_privilege`` rather than the ``duplicate_object`` the handler
# catches. On a lane where the role already exists this is a no-op.
_APP_DASHBOARD_ROLE_SQL = """
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_dashboard') THEN
    BEGIN
      CREATE ROLE app_dashboard WITH
        NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION;
    EXCEPTION
      WHEN duplicate_object OR unique_violation THEN
        NULL;
    END;
  END IF;
END;
$$;
"""

_MIRROR_DDL = """
CREATE TABLE IF NOT EXISTS tenant_registry_mirror (
    tenant_slug         TEXT PRIMARY KEY,
    tenant_uuid         UUID NOT NULL,
    display_name        TEXT,
    status              TEXT NOT NULL,
    registry_created_at TIMESTAMPTZ,
    observed_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_event_id     TEXT
);
"""

_SHAPE_SQL = """
SELECT c.data_type,
       c.column_default,
       k.relrowsecurity,
       k.relforcerowsecurity,
       pg_get_userbyid(k.relowner) AS table_owner,
       current_user                AS connected_as,
       (SELECT pg_get_expr(polqual, polrelid)
          FROM pg_policy WHERE polrelid = k.oid LIMIT 1) AS policy_using
  FROM information_schema.columns c
  JOIN pg_class k ON k.oid = (c.table_schema || '.delegation_events')::regclass
 WHERE c.table_schema = $1
   AND c.table_name = 'delegation_events'
   AND c.column_name = 'tenant_id';
"""


def _test_schema_safe_sql(raw_sql: str) -> str:
    return raw_sql.replace("CREATE INDEX CONCURRENTLY", "CREATE INDEX")


def _live_migration_files() -> list[Path]:
    return [
        path
        for path in sorted(_MIGRATIONS_DIR.glob("*.sql"))
        if path.name not in _UNAPPLIED_ON_ONEX_DEV
    ]


def _dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping the OMN-16831 lab proof"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


def _bootstrap() -> str:
    servers = os.environ.get("OMN16831_LAB_BOOTSTRAP", "").strip()
    if not servers:
        pytest.skip(
            "OMN16831_LAB_BOOTSTRAP not set -- skipping the OMN-16831 lab proof. "
            "Point it at a lab broker (the .201 dev lane's Redpanda), never at a "
            "governed lane."
        )
    return servers


def _envelope(payload: dict[str, Any]) -> bytes:
    """The envelope shape read off the live broker for both delegations."""
    return json.dumps(
        {
            "payload": payload,
            "envelope_id": str(uuid.uuid4()),
            "envelope_version": {"major": 1, "minor": 0, "patch": 0, "build": None},
        }
    ).encode("utf-8")


def _quality_gate_payload() -> dict[str, Any]:
    return {
        "correlation_id": LAB_CORRELATION_ID,
        "passed": False,
        "fail_category": "fail_deterministic",
        "quality_score": 0.40,
        "actual_score": 0.40,
        "failure_reasons": ["score_below_required_bar"],
        "score_source": "deterministic_acceptance",
    }


def _delegation_failed_payload() -> dict[str, Any]:
    return {
        "correlation_id": LAB_CORRELATION_ID,
        "tenant_id": str(LAB_TENANT_UUID),
        "task_type": "code-review",
        "model_used": "glm-5.2",
        "content": "",
        "quality_passed": False,
        "quality_score": 0.40,
        "latency_ms": 1800,
        "prompt_tokens": 210,
        "completion_tokens": 0,
        "cumulative_attempt_cost": 0.0142,
        "cost_tier_name": "cheap_cloud",
    }


async def _round_trip(
    schema_dsn: str, bootstrap: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Publish both events, feed the fetched records to the deployed path.

    Returns the ``delegation_events`` readback and the DLQ envelopes the writer
    produced, so a residual refusal is asserted rather than left invisible.
    """
    from aiokafka import AIOKafkaConsumer, AIOKafkaProducer, TopicPartition

    dlq: list[dict[str, Any]] = []

    async def _capture_dlq(topic: str, value: bytes) -> None:
        # The runner publishes aggregate snapshot deltas through this same hook,
        # so select the quarantine topic rather than counting every publish.
        if not topic.startswith("onex.dlq."):
            return
        dlq.append({"topic": topic, **json.loads(value.decode("utf-8"))})

    runner = DelegationProjectionRunner(publish_fn=_capture_dlq)
    qg_topic = runner._topic_quality_gate_result
    failed_topic = runner._topic_delegation_failed
    assert qg_topic, "contract must declare a quality-gate-result topic"
    assert failed_topic, "contract must declare a delegation-failed topic"

    consumer = AIOKafkaConsumer(
        bootstrap_servers=bootstrap,
        enable_auto_commit=False,
        auto_offset_reset="latest",
        group_id=None,
    )
    await consumer.start()
    producer = AIOKafkaProducer(bootstrap_servers=bootstrap)
    await producer.start()
    try:
        parts = [
            TopicPartition(topic, partition)
            for topic in (qg_topic, failed_topic)
            for partition in (consumer.partitions_for_topic(topic) or {0})
        ]
        consumer.assign(parts)
        for part in parts:
            await consumer.seek_to_end(part)

        await producer.send_and_wait(qg_topic, _envelope(_quality_gate_payload()))
        await producer.send_and_wait(
            failed_topic, _envelope(_delegation_failed_payload())
        )

        runner.bind_projection_database_url(schema_dsn)
        await runner.db.connect()

        seen = 0
        deadline = asyncio.get_running_loop().time() + 60
        while seen < 2 and asyncio.get_running_loop().time() < deadline:
            batches = await consumer.getmany(timeout_ms=2000)
            for records in batches.values():
                for record in records:
                    body = json.loads(record.value.decode("utf-8"))
                    if body["payload"]["correlation_id"] != LAB_CORRELATION_ID:
                        continue
                    # THE DEPLOYED PATH. _handle_message unwraps the envelope,
                    # dispatches, classifies errors and decides offset policy.
                    await runner._handle_message(record)
                    seen += 1
        assert seen == 2, f"expected both published records back, fetched {seen}"

        rows = await runner.db.execute(
            "SELECT correlation_id, tenant_id, quality_gate_passed, "
            "quality_gate_detail, actual_score, score_source "
            "FROM delegation_events WHERE correlation_id = $1",
            LAB_CORRELATION_ID,
            tenant=str(LAB_TENANT_UUID),
        )
        return rows, dlq
    finally:
        await producer.stop()
        await consumer.stop()
        await runner.db.close()


@pytest.mark.integration
def test_a_real_delegation_becomes_a_real_row(
    capsys: pytest.CaptureFixture[str],
) -> None:
    dsn = _dsn()
    bootstrap = _bootstrap()
    loop = asyncio.new_event_loop()
    conn: asyncpg.Connection | None = None
    schema = f"omn16831_{uuid.uuid4().hex[:12]}"
    try:
        try:
            conn = loop.run_until_complete(asyncpg.connect(dsn))
        except (OSError, asyncpg.PostgresError) as exc:
            pytest.skip(f"no reachable Postgres for the OMN-16831 lab proof: {exc}")
        assert conn is not None
        try:
            loop.run_until_complete(conn.execute(f"CREATE SCHEMA {schema}"))
            loop.run_until_complete(
                conn.execute(f"SET search_path TO {schema}, public")
            )
            loop.run_until_complete(conn.execute(_APP_DASHBOARD_ROLE_SQL))
            for path in _live_migration_files():
                loop.run_until_complete(
                    conn.execute(
                        _test_schema_safe_sql(path.read_text(encoding="utf-8"))
                    )
                )
            loop.run_until_complete(conn.execute(_MIRROR_DDL))
            loop.run_until_complete(
                conn.execute(
                    f"INSERT INTO {TENANT_REGISTRY_MIRROR_TABLE} "
                    "(tenant_slug, tenant_uuid, status) VALUES ($1, $2, 'active')",
                    LAB_TENANT_SLUG,
                    LAB_TENANT_UUID,
                )
            )

            shape = loop.run_until_complete(conn.fetch(_SHAPE_SQL, schema))
            schema_dsn = f"{dsn}?options=-c%20search_path%3D{schema}%2Cpublic"
            rows, dlq = loop.run_until_complete(_round_trip(schema_dsn, bootstrap))
        finally:
            loop.run_until_complete(
                conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            )
            loop.run_until_complete(conn.close())
    finally:
        loop.close()

    with capsys.disabled():
        print(f"\nOMN-16831 LAB SCHEMA SHAPE schema={schema}")
        print(json.dumps([dict(r) for r in shape], default=str, indent=2))
        print("OMN-16831 LAB READBACK delegation_events")
        print(json.dumps(rows, default=str, indent=2))
        print("OMN-16831 LAB DLQ (residual refusals, see the module docstring)")
        print(
            json.dumps(
                [{k: v for k, v in e.items() if k != "original_message"} for e in dlq],
                default=str,
                indent=2,
            )
        )

    assert len(rows) == 1, (
        "RED before the fix: both source events are rejected inside the writer "
        "-- one on _envelope, one on the tenant lookup -- and this SELECT "
        "returns zero rows, which is the onex-dev symptom exactly"
    )
    row = rows[0]
    assert row["correlation_id"] == LAB_CORRELATION_ID
    # The column is TEXT on this lane (see the shape control below), so Postgres
    # hands back a str. What matters is WHICH identifier it stored: the UUID the
    # registry mirror recorded, not the slug and not the house default. Before
    # defect B was fixed this write never happened at all, because a UUID was
    # being matched against the mirror's ``tenant_slug`` column.
    assert row["tenant_id"] == str(LAB_TENANT_UUID), (
        f"expected the registry-resolved identity, got {row['tenant_id']!r}"
    )
    # THE HARNESS REPRODUCES THE LANE. If any of these drifts, every assertion
    # above is about a schema onex-dev does not have, and the proof is void.
    assert len(shape) == 1
    lane = dict(shape[0])
    assert lane["data_type"] == "text"
    assert lane["column_default"] == "'omninode'::text"
    assert lane["relrowsecurity"] is True
    assert lane["relforcerowsecurity"] is True
    assert lane["policy_using"] == (
        "(tenant_id = current_setting('app.tenant_id'::text, true))"
    )

    # THE NAMED RESIDUAL. The quality-gate-result event carries no tenant at
    # all, so its row omits ``tenant_id`` and Postgres applies the column
    # DEFAULT 'omninode' -- while ``resolve_write_tenant(None, ...)`` sets the
    # RLS GUC to the house tenant's UUID form, because
    # ``_UUID_CONVERTED_TABLES`` asserts this relation was converted and on
    # onex-dev it was not. Stored value and GUC disagree, so the policy refuses
    # the write. That refusal is DURABLE and RECOVERABLE (it is on the DLQ) and
    # it does NOT cost the row: the terminal event carries the same
    # ``quality_gate_passed`` verdict and writes it. This assertion exists so
    # the residual is a recorded, tested fact rather than something a later
    # reader has to rediscover. It is OMN-16831's remaining link, not a defect
    # this file leaves unnoticed.
    refusals = [e["failure_reason"] for e in dlq]
    assert len(refusals) == 1, f"expected exactly the one known residual: {refusals}"
    assert "row-level security policy" in refusals[0]
