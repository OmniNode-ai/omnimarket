# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17210: real-Postgres write-path gate for the receipt-gate projection
(``HandlerReceiptGateProjectionRunner``, node_projection_receipt_gate).

Why this module exists, and why it is NOT redundant with
``src/omnimarket/nodes/node_projection_receipt_gate/tests/test_handler_receipt_gate.py``:
that suite drives the same ``project_event()`` entrypoint against an
``AsyncMock`` DB double, which accepts a bound parameter of ANY Python type --
an ISO ``str`` binds just as "successfully" as a real ``datetime``. Only a real
Postgres connection enforces column types through asyncpg's extended query
protocol. That gap is exactly how the OMN-15905 str-where-TIMESTAMPTZ defect
reached a merged, deployed, CrashLoopBackOff-ing runtime with every layer of
mock-DB coverage green.

``receipt_gate_rows`` has the same trap twice over: ``observed_at`` is
``TIMESTAMPTZ`` (needs a ``datetime``) while ``signed_at`` is deliberately
``TEXT`` (needs the ISO ``str``, and would be REJECTED if a ``datetime`` were
bound). A mock DB cannot tell those two columns apart.

Real Postgres, never SQLite: SQLite's loosely-affinity-typed columns accept a
Python ``str`` into a column declared TIMESTAMP without complaint -- the exact
hole this gate exists to close -- and asyncpg against real Postgres is the
actual runtime the deployed writer uses (``AsyncpgAdapter``).

Harness pattern (asyncpg ``_connect_or_skip`` / disposable schema) mirrors
``tests/test_omn15909_real_postgres_projection_write_path_gate.py``: SKIPS
(never ERRORs) without a reachable database, and provisions its own throwaway
schema so parallel runs never collide.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_receipt_gate.handlers.handler_receipt_gate import (
    HandlerReceiptGateProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta

RECEIPT_TOPIC = "onex.evt.omnimarket.verification-receipt-completed.v1"
EVIDENCE_TOPIC = "onex.evt.omnimarket.evidence-validated.v1"

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_receipt_gate"
    / "migrations"
)


def _live_migration_files() -> list[Path]:
    """Every migration for this node, in the numeric/lexical order the real
    migration runner applies them -- the FULL live set, not a hand-picked
    subset, so a column added by a later migration cannot silently fall out of
    the schema this gate exercises."""
    return sorted(_MIGRATIONS_DIR.glob("*.sql"))


def _base_dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip(
            "POSTGRES_PASSWORD not set -- skipping OMN-17210 real-Postgres "
            "receipt-gate write-path gate"
        )
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for OMN-17210 write-path gate: {exc}")


@asynccontextmanager
async def _provisioned_runner() -> AsyncIterator[
    tuple[HandlerReceiptGateProjectionRunner, asyncpg.Connection, str]
]:
    """Provision a disposable schema carrying the live migrated receipt-gate
    schema, bind a real asyncpg-backed runner to it, and yield
    ``(runner, admin_conn, schema)``."""
    admin_conn = await _connect_or_skip()
    schema = f"omn17210_{uuid4().hex[:16]}"
    pool: asyncpg.Pool | None = None
    try:
        await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin_conn.execute(f"CREATE SCHEMA {schema}")
        await admin_conn.execute(f"SET search_path TO {schema}, public")
        for migration_path in _live_migration_files():
            await admin_conn.execute(migration_path.read_text(encoding="utf-8"))

        pool = await asyncpg.create_pool(
            _base_dsn(),
            min_size=1,
            max_size=3,
            server_settings={"search_path": f"{schema},public"},
        )
        adapter = AsyncpgAdapter(dsn=_base_dsn())
        adapter._pool = pool  # type: ignore[attr-defined]

        runner = HandlerReceiptGateProjectionRunner()
        runner._db = adapter  # type: ignore[assignment]

        yield runner, admin_conn, schema
    finally:
        if pool is not None:
            with contextlib.suppress(Exception):
                await pool.close()
        with contextlib.suppress(Exception):
            await admin_conn.execute("SET search_path TO public")
            await admin_conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin_conn.close()


def _meta(topic: str) -> MessageMeta:
    return MessageMeta(partition=0, offset=17, fallback_id="fallback", topic=topic)


def _receipt_payload() -> dict[str, Any]:
    """The real ``verification-receipt-completed.v1`` shape, with a ``checks``
    list so the reducer fans it out to one row per dimension."""
    return {
        "task_id": "OMN-17210",
        "pr_number": 2222,
        "repo": "omnimarket",
        "verifier": "node_verification_receipt_generator",
        "verified_at": "2026-08-30T12:00:00+00:00",
        "overall_pass": True,
        "checks": [
            {"dimension": "ci_checks", "passed": True, "summary": "all green"},
            {"dimension": "pytest", "passed": False, "summary": "2 failed"},
        ],
    }


@pytest.mark.integration
class TestRealPostgresReceiptGateWritePath:
    """The write path must land correctly-TYPED rows in real Postgres."""

    async def test_verification_receipt_lands_one_typed_row_per_check(self) -> None:
        async with _provisioned_runner() as (runner, admin_conn, schema):
            assert await runner.project_event(
                RECEIPT_TOPIC, _receipt_payload(), _meta(RECEIPT_TOPIC)
            )

            rows = await admin_conn.fetch(
                f"SELECT name, pass, detail, pr_ref, worker, verifier, "
                f"evidence_count, evidence_hash, signed_at, observed_at "
                f"FROM {schema}.receipt_gate_rows ORDER BY name"
            )

            assert [r["name"] for r in rows] == ["ci_checks", "pytest"]
            assert [r["pass"] for r in rows] == [True, False]
            # The whole point of a real connection: asyncpg decodes the
            # TIMESTAMPTZ column back to a tz-aware datetime, which is only
            # possible because a datetime (not an ISO string) was bound.
            for row in rows:
                assert isinstance(row["observed_at"], datetime)
                assert row["observed_at"].tzinfo is not None
                # signed_at is deliberately TEXT in migration 0000 -- the
                # inverse trap. The reducer sets it to ``verified_at
                # .isoformat()``, a str; binding the datetime it was derived
                # from would be rejected by this same column.
                assert isinstance(row["signed_at"], str)
                assert row["signed_at"].startswith("2026-08-30T12:00:00")
                assert row["pr_ref"] == "OMN-17210 / #2222"
                assert row["verifier"] == "node_verification_receipt_generator"
                # The per-check rows carry no evidence roll-up today -- the
                # reducer hardcodes both to None. Asserted so a future reducer
                # change that starts populating them cannot land silently.
                assert row["evidence_count"] is None
                assert row["evidence_hash"] is None

    async def test_evidence_validated_lands_an_occ_row(self) -> None:
        async with _provisioned_runner() as (runner, admin_conn, schema):
            assert await runner.project_event(
                EVIDENCE_TOPIC,
                {
                    "ticket_id": "OMN-17210",
                    "validation_state": "PASSED",
                    "evidence_lifecycle_state": "VALIDATED",
                },
                _meta(EVIDENCE_TOPIC),
            )

            names = [
                r["name"]
                for r in await admin_conn.fetch(
                    f"SELECT name FROM {schema}.receipt_gate_rows"
                )
            ]
            assert names == ["occ-evidence"]

    async def test_redelivery_of_the_same_event_does_not_duplicate_rows(self) -> None:
        """Kafka delivery is at-least-once and the offset is committed after
        the write, so a rebalance redelivers. The ``WHERE NOT EXISTS`` guard in
        ``_insert_row`` is the only thing between that and a duplicated row --
        ``receipt_gate_rows`` has no natural key to ``ON CONFLICT`` against
        (its sole unique constraint is the ``id BIGSERIAL`` PK). A mock DB
        cannot prove this at all: it never reads back what it wrote."""
        async with _provisioned_runner() as (runner, admin_conn, schema):
            payload = _receipt_payload()
            assert await runner.project_event(
                RECEIPT_TOPIC, dict(payload), _meta(RECEIPT_TOPIC)
            )
            assert await runner.project_event(
                RECEIPT_TOPIC, dict(payload), _meta(RECEIPT_TOPIC)
            )

            count = await admin_conn.fetchval(
                f"SELECT count(*) FROM {schema}.receipt_gate_rows"
            )
            # Two check dimensions, projected twice, still two rows.
            assert count == 2

    async def test_unsubscribed_topic_writes_nothing(self) -> None:
        """The reducer's ``_best_effort_row`` fallback would happily write a
        junk row for an unrecognised shape and hide a wiring bug behind a
        widget that looks populated. The runner refuses before the write."""
        async with _provisioned_runner() as (runner, admin_conn, schema):
            assert not await runner.project_event(
                "onex.evt.omnimarket.not-a-receipt-topic.v1",
                {"anything": "at all"},
                _meta("onex.evt.omnimarket.not-a-receipt-topic.v1"),
            )

            count = await admin_conn.fetchval(
                f"SELECT count(*) FROM {schema}.receipt_gate_rows"
            )
            assert count == 0


@pytest.mark.integration
class TestRedProofObservedAtColumnType:
    """RED/GREEN on the exact OMN-15905 defect class, isolated from whichever
    higher-level caller happens to be correct on a given day: real Postgres
    must REJECT an ISO string bound to ``observed_at`` and ACCEPT a datetime.
    """

    async def test_iso_string_into_timestamptz_is_rejected(self) -> None:
        async with _provisioned_runner() as (runner, _admin_conn, schema):
            with pytest.raises(asyncpg.exceptions.DataError):
                await runner.db.execute(
                    f"INSERT INTO {schema}.receipt_gate_rows "
                    "(name, pass, detail, observed_at) VALUES ($1, $2, $3, $4)",
                    "red-proof",
                    True,
                    "",
                    "2026-08-30T12:00:00+00:00",
                )

    async def test_datetime_into_timestamptz_is_accepted(self) -> None:
        async with _provisioned_runner() as (runner, admin_conn, schema):
            await runner.db.execute(
                f"INSERT INTO {schema}.receipt_gate_rows "
                "(name, pass, detail, observed_at) VALUES ($1, $2, $3, $4)",
                "green-proof",
                True,
                "",
                datetime.now(UTC),
            )
            count = await admin_conn.fetchval(
                f"SELECT count(*) FROM {schema}.receipt_gate_rows "
                "WHERE name = 'green-proof'"
            )
            assert count == 1
