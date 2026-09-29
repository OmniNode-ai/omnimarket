# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres UPSERT proof for board probe results (OMN-19937).

A database double cannot evaluate the ``ON CONFLICT ... WHERE`` ordering guard
or reject values with the wrong PostgreSQL type. This suite applies the real
migration in a disposable schema and drives the actual writer statement
through asyncpg. It skips cleanly when the INTEGRATION_POSTGRES credentials are
not configured.
"""

from __future__ import annotations

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

from omnimarket.nodes.node_projection_board_probe_results.contract_topics import (
    TOPIC_BOARD_PROBE_RESULT,
)
from omnimarket.nodes.node_projection_board_probe_results.handlers import (
    handler_board_probe_results_writer as writer_module,
)
from omnimarket.nodes.node_projection_board_probe_results.handlers.handler_board_probe_results_writer import (
    BoardProbeResultsProjectionWriter,
)
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.integration

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_board_probe_results"
    / "migrations"
    / "0000_create_board_probe_results.sql"
)
_OLDER = datetime(2026, 9, 28, 14, 0, tzinfo=UTC)
_NEWER = datetime(2026, 9, 28, 15, 0, tzinfo=UTC)


def _integration_postgres_dsn() -> str | None:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        return None
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    database = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}@"
        f"{host}:{port}/{database}"
    )


async def _connect_or_skip() -> asyncpg.Connection:
    dsn = _integration_postgres_dsn()
    if dsn is None:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD/POSTGRES_PASSWORD unset -- skipping "
            "the OMN-19937 real-Postgres writer proof"
        )
    try:
        return await asyncpg.connect(dsn)
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for the OMN-19937 writer proof: {exc}")
        raise AssertionError("pytest.skip always raises") from exc


class _ConnectionDb:
    """Expose the writer's adapter protocol on one schema-scoped connection."""

    def __init__(self, connection: asyncpg.Connection) -> None:
        self._connection = connection

    async def execute(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return [dict(row) for row in await self._connection.fetch(sql, *args)]


@asynccontextmanager
async def _migrated_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[BoardProbeResultsProjectionWriter, asyncpg.Connection, str]]:
    connection = await _connect_or_skip()
    schema = f"omn19937_{uuid4().hex[:12]}"
    try:
        await connection.execute(f"CREATE SCHEMA {schema}")
        migration = _MIGRATION.read_text(encoding="utf-8").replace(
            "omninode_internal.", f"{schema}."
        )
        await connection.execute(migration)
        monkeypatch.setattr(
            writer_module,
            "_UPSERT",
            writer_module._UPSERT.replace("omninode_internal.", f"{schema}."),
        )

        writer = BoardProbeResultsProjectionWriter()
        writer._db = _ConnectionDb(connection)  # type: ignore[assignment]

        async def _discard_snapshot(exposure: Any, **kwargs: Any) -> bool:
            return True

        writer.publish_snapshot_delta = _discard_snapshot  # type: ignore[method-assign]
        yield writer, connection, schema
    finally:
        await connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await connection.close()


def _payload(
    *, execution_id: str, outcome: str, finished_at: datetime
) -> dict[str, Any]:
    return {
        "check_id": "branch-protection",
        "subject_kind": "pull_request",
        "subject": "OmniNode-ai/omnimarket#321",
        "repo": "OmniNode-ai/omnimarket",
        "sha": "a" * 40,
        "surface_instance": "github-main",
        "execution_id": execution_id,
        "outcome": outcome,
        "reasons": [] if outcome == "PASS" else ["probe did not pass"],
        "evidence_items": ["probe://board/result"],
        "finished_at": finished_at.isoformat(),
    }


async def _project(
    writer: BoardProbeResultsProjectionWriter,
    *,
    execution_id: str,
    outcome: str,
    finished_at: datetime,
    offset: int,
) -> None:
    accepted = await writer.project_event(
        TOPIC_BOARD_PROBE_RESULT,
        _payload(
            execution_id=execution_id,
            outcome=outcome,
            finished_at=finished_at,
        ),
        MessageMeta(
            partition=0,
            offset=offset,
            fallback_id=f"{execution_id}-{offset}",
            topic=TOPIC_BOARD_PROBE_RESULT,
        ),
    )
    assert accepted is True


@pytest.mark.integration
@pytest.mark.parametrize(
    "delivery_order",
    [("older", "newer"), ("newer", "older")],
)
async def test_one_key_converges_on_later_finished_at_in_both_delivery_orders(
    monkeypatch: pytest.MonkeyPatch,
    delivery_order: tuple[str, str],
) -> None:
    events = {
        "older": {
            "outcome": "FAIL",
            "finished_at": _OLDER,
            "offset": 90,
        },
        "newer": {
            "outcome": "PASS",
            "finished_at": _NEWER,
            "offset": 3,
        },
    }
    async with _migrated_writer(monkeypatch) as (writer, connection, schema):
        for label in delivery_order:
            event = events[label]
            await _project(
                writer,
                execution_id="probe-run-same-key",
                outcome=str(event["outcome"]),
                finished_at=event["finished_at"],  # type: ignore[arg-type]
                offset=int(event["offset"]),
            )

        rows = await connection.fetch(
            f"SELECT execution_id, outcome, finished_at, source_offset "
            f"FROM {schema}.board_probe_results"
        )
        assert len(rows) == 1
        assert rows[0]["execution_id"] == "probe-run-same-key"
        assert rows[0]["outcome"] == "PASS"
        assert rows[0]["finished_at"] == _NEWER
        assert rows[0]["source_offset"] == 3


@pytest.mark.integration
async def test_a_different_execution_id_creates_a_second_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with _migrated_writer(monkeypatch) as (writer, connection, schema):
        await _project(
            writer,
            execution_id="probe-run-1",
            outcome="FAIL",
            finished_at=_OLDER,
            offset=1,
        )
        await _project(
            writer,
            execution_id="probe-run-2",
            outcome="PASS",
            finished_at=_NEWER,
            offset=2,
        )

        rows = await connection.fetch(
            f"SELECT execution_id, outcome FROM {schema}.board_probe_results "
            "ORDER BY execution_id"
        )
        assert [(row["execution_id"], row["outcome"]) for row in rows] == [
            ("probe-run-1", "FAIL"),
            ("probe-run-2", "PASS"),
        ]
