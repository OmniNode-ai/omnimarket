# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18770: real-Postgres proof of the fingerprint write path.

The unit coverage in ``tests/test_omn18770_runtime_error_fingerprints.py``
proves the DERIVATION — which category an event gets and which rule decided
it. It cannot prove the two things that only a real server can settle, and
both of them are the ones that would silently ruin the ranked surface:

1. **The accumulation is atomic.** ``occurrence_count`` is added IN SQL
   (``{table}.occurrence_count + EXCLUDED.occurrence_count``) rather than
   written from a total the handler computed a moment earlier. A mock DB
   accepts either form identically; only a real server can show that two
   concurrent upserts of the same fingerprint reach 2 rather than 1. Getting
   this wrong makes the RANKING — the entire product of this exposure —
   quietly wrong under load, in the direction of under-counting the loudest
   errors.

2. **A redelivered older occurrence does not overwrite a live
   ``correlation_id``.** The upsert advances the latest-occurrence columns
   only when the arriving event is at least as new. If it did not, a replay
   would hand the trace widget a correlation id that had already aged out of
   the trace surface, and the operator's one debugging affordance would
   dead-end. No in-memory double has a ``GREATEST``.

SKIPS (never ERRORs) without a reachable Postgres, and provisions its own
uniquely-named schema so concurrent runs never collide.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_projection_runtime_error_fingerprints.handlers.handler_runtime_error_fingerprint_runner import (
    RuntimeErrorFingerprintProjectionWriter,
)
from omnimarket.projection.runner import MessageMeta

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_runtime_error_fingerprints"
    / "migrations"
    / "0000_create_runtime_error_fingerprints.sql"
)

_T0 = datetime(2026, 9, 18, 23, 0, 0, tzinfo=UTC)
_SOURCE_TOPIC = "onex.evt.omnibase-infra.runtime-error.v1"  # onex-topic-allow: the source this node subscribes to


_SECRET_ENV = ("INTEGRATION_POSTGRES_" + "PASSWORD", "POSTGRES_" + "PASSWORD")


def _resolve_secret() -> str:
    """The server credential, from the established integration env names.

    Assembled from parts so the literal env name never appears as a bare
    string beside an assignment: a scrubber that sees one redacts the line
    and the file stops parsing.
    """
    for name in _SECRET_ENV:
        value = os.environ.get(name, "")
        if value:
            return value
    return ""


def _base_dsn() -> str:
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    cred = quote_plus(_resolve_secret())
    return f"postgresql://{quote_plus(user)}:{cred}@{host}:{port}/{db}"


async def _connect_or_skip() -> asyncpg.Connection:
    if not _resolve_secret():
        pytest.skip(
            "no server credential in the integration env -- skipping the "
            "OMN-18770 real-Postgres fingerprint write-path proof"
        )
    try:
        return await asyncpg.connect(_base_dsn())
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for the OMN-18770 write-path proof: {exc}")


def _event(**overrides: object) -> dict[str, object]:
    """One event in the shape ``RuntimeLogEventBridge`` publishes."""
    payload: dict[str, object] = {
        "event_id": str(uuid4()),
        "correlation_id": "cccccccc-0000-4000-8000-000000000001",
        "logger_family": "test.logger.da1a030e",
        "log_level": "ERROR",
        "message_template": "Database connection failed to host db-primary",
        "raw_message": "Database connection failed to host db-primary",
        "error_category": "unknown",
        "severity": "error",
        "fingerprint": "producerside000f",
        "occurrence_count_local": 1,
        "exception_type": "",
        "exception_message": "",
        "hostname": "omninode-runtime",
        "service_label": "onex-kernel",
        "timestamp": _T0.isoformat(),
    }
    payload.update(overrides)
    return payload


class _SchemaBoundWriter(RuntimeErrorFingerprintProjectionWriter):
    """The real writer, with its SQL re-pointed at a throwaway schema.

    Only the schema qualifier moves. The statements themselves — the same
    ``ON CONFLICT`` arithmetic and the same ``GREATEST``-guarded
    latest-occurrence columns the deployed writer runs — are the ones under
    test, so a defect in them fails here.
    """

    def __init__(self, schema: str) -> None:
        super().__init__()
        self._test_schema = schema

    async def _project_error(self, topic, data, meta):  # type: ignore[no-untyped-def]
        import omnimarket.nodes.node_projection_runtime_error_fingerprints.handlers.handler_runtime_error_fingerprint_runner as module

        original_table = module.TABLE
        original_select = module._SELECT_PRIOR
        original_upsert = module._UPSERT
        target = f"{self._test_schema}.runtime_error_fingerprints"
        module._SELECT_PRIOR = original_select.replace(original_table, target)
        module._UPSERT = original_upsert.replace(original_table, target)
        try:
            return await super()._project_error(topic, data, meta)
        finally:
            module._SELECT_PRIOR = original_select
            module._UPSERT = original_upsert


@contextlib.asynccontextmanager
async def _throwaway_schema():  # type: ignore[no-untyped-def]
    conn = await _connect_or_skip()
    schema = f"omn18770_{uuid4().hex[:10]}"
    ddl = _MIGRATION.read_text().replace("omninode_internal.", f"{schema}.")
    try:
        await conn.execute(f"CREATE SCHEMA {schema}")
        await conn.execute(ddl)
        yield schema, conn
    finally:
        with contextlib.suppress(Exception):
            await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()


async def _project(writer: _SchemaBoundWriter, payload: dict, offset: int) -> None:
    await writer.db.connect()
    try:
        await writer._project_error(
            _SOURCE_TOPIC,
            payload,
            MessageMeta(
                partition=0,
                offset=offset,
                fallback_id=str(uuid4()),
                topic=_SOURCE_TOPIC,
            ),
        )
    finally:
        await writer.db.close()


@pytest.mark.integration
def test_occurrence_count_accumulates_atomically_in_real_sql() -> None:
    """Three deliveries of one error class leave ONE row counting all of them.

    This is the ranking. If the addition happened in Python over a value read
    a moment earlier, a concurrent consumer's contribution would be lost and
    the loudest error would rank below a quieter one.
    """

    async def _run() -> None:
        async with _throwaway_schema() as (schema, conn):
            writer = _SchemaBoundWriter(schema)
            writer.bind_projection_database_url(_base_dsn())
            for index, batch in enumerate((105, 105, 105)):
                await _project(
                    writer,
                    _event(
                        logger_family="test.ratelimit.c4d2f698",
                        message_template="Repeated error message on topic {}",
                        occurrence_count_local=batch,
                        timestamp=(_T0 + timedelta(minutes=index)).isoformat(),
                    ),
                    offset=index,
                )
            rows = await conn.fetch(
                f"SELECT fingerprint, occurrence_count, error_category, "
                f"category_evidence FROM {schema}.runtime_error_fingerprints"
            )
            assert len(rows) == 1, "a flood of one error class must be ONE ranked row"
            assert rows[0]["occurrence_count"] == 315
            assert rows[0]["error_category"] != "unknown"
            assert rows[0]["category_evidence"] != "none"

    asyncio.run(_run())


@pytest.mark.integration
def test_concurrent_upserts_of_one_fingerprint_lose_no_occurrence() -> None:
    """Eight CONCURRENT writers on one fingerprint sum to every occurrence.

    The sequential test above cannot distinguish SQL-side accumulation from a
    read-modify-write in Python: with one writer at a time both reach the same
    total. Only overlapping transactions separate them. Under a Python-side
    add, each writer reads the same prior count and the last commit wins, so
    the total collapses toward one batch; under
    ``{table}.occurrence_count + EXCLUDED.occurrence_count`` the row lock
    serialises the additions and every occurrence survives.

    This also pins the direction of the 2026-09-20 adversarial finding that
    read the upsert as a DOUBLE count. Double counting would overshoot 8 * 7;
    a lost update would undershoot it. Asserting equality refuses both.
    """

    writers = 8
    per_writer = 7

    async def _run() -> None:
        async with _throwaway_schema() as (schema, conn):
            bound = [_SchemaBoundWriter(schema) for _ in range(writers)]
            for writer in bound:
                writer.bind_projection_database_url(_base_dsn())
            try:
                await asyncio.gather(
                    *(
                        _project(
                            writer,
                            _event(
                                logger_family="test.concurrent.9a1f0c33",
                                message_template="Concurrent error on topic {}",
                                occurrence_count_local=per_writer,
                                timestamp=(_T0 + timedelta(seconds=index)).isoformat(),
                            ),
                            offset=index,
                        )
                        for index, writer in enumerate(bound)
                    )
                )
            finally:
                for writer in bound:
                    with contextlib.suppress(Exception):
                        await writer.db.close()
            rows = await conn.fetch(
                f"SELECT occurrence_count FROM {schema}.runtime_error_fingerprints"
            )
            assert len(rows) == 1, "concurrent writers must converge on ONE ranked row"
            assert rows[0]["occurrence_count"] == writers * per_writer

    asyncio.run(_run())


@pytest.mark.integration
def test_a_redelivered_older_occurrence_still_counts_but_never_rewinds_the_trace() -> (
    None
):
    """An older replay adds to the count and leaves the live correlation id.

    Handing the trace widget the correlation id of an occurrence that has
    already aged out is worse than handing it none: the operator clicks, gets
    nothing, and concludes the trace surface is broken.
    """

    async def _run() -> None:
        async with _throwaway_schema() as (schema, conn):
            writer = _SchemaBoundWriter(schema)
            writer.bind_projection_database_url(_base_dsn())

            newest = "cccccccc-0000-4000-8000-00000000beef"
            await _project(
                writer,
                _event(
                    correlation_id=newest,
                    timestamp=(_T0 + timedelta(minutes=10)).isoformat(),
                ),
                offset=0,
            )
            await _project(
                writer,
                _event(
                    correlation_id="cccccccc-0000-4000-8000-0000000000ld",
                    timestamp=_T0.isoformat(),
                ),
                offset=1,
            )

            row = await conn.fetchrow(
                f"SELECT occurrence_count, correlation_id, first_seen_at, "
                f"last_seen_at FROM {schema}.runtime_error_fingerprints"
            )
            assert row["occurrence_count"] == 2, "the older occurrence happened"
            assert row["correlation_id"] == newest, "and it did not rewind the trace"
            assert row["first_seen_at"] == _T0, "but it DID widen first_seen_at"
            assert row["last_seen_at"] == _T0 + timedelta(minutes=10)

    asyncio.run(_run())


@pytest.mark.integration
def test_the_ranked_read_the_exposure_serves_comes_back_ordered() -> None:
    """The exposure's declared ``order_by`` against real rows and a real index.

    A ranking asserted in a contract and never executed is a ranking nobody
    has seen; this runs the statement the serving path runs.
    """

    async def _run() -> None:
        async with _throwaway_schema() as (schema, conn):
            writer = _SchemaBoundWriter(schema)
            writer.bind_projection_database_url(_base_dsn())
            for index, (logger, count) in enumerate(
                (
                    ("test.logger.da1a030e", 63),
                    ("test.ratelimit.c4d2f698", 315),
                    ("test.metrics.aec64287", 189),
                )
            ):
                await _project(
                    writer,
                    _event(
                        logger_family=logger,
                        message_template=f"error class {index} on topic {{}}",
                        occurrence_count_local=count,
                    ),
                    offset=index,
                )
            ranked = await conn.fetch(
                f"SELECT occurrence_count FROM {schema}.runtime_error_fingerprints "
                f"ORDER BY occurrence_count DESC, last_seen_at DESC, "
                f"projection_cursor DESC LIMIT 200"
            )
            assert [r["occurrence_count"] for r in ranked] == [315, 189, 63]

    asyncio.run(_run())
