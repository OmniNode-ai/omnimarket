# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19987: a step whose row was stored but whose snapshot was never published.

The writer stores a step in one transaction and publishes its snapshot delta
afterwards. If the publish fails, the runtime redelivers the event, the insert
conflicts on the step's key, and the step is already durable. A writer that
returns there without publishing leaves the bus-backed ``run-trace.v1`` without
that step for good, because every step is published exactly once.

These tests drive the writer's own ``_project_step`` against a stand-in pool
that keeps rows by (tenant, run, sequence), so they need no database. The same
sequence against a real Postgres is in ``tests/test_omn19987_run_trace_real_postgres.py``.

Failure modes each test is written against:

* the publish raises after the row is committed, and the error is swallowed
  (the runtime would then acknowledge an event whose snapshot never went out);
* the redelivered event finds its row and publishes nothing;
* the redelivery publishes the redelivered event's content instead of the
  stored fact, so a changed redelivery rewrites what readers see;
* a redelivery for a step this tenant never stored publishes something anyway.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_projection_run_trace.handlers.handler_run_trace_writer import (
    RunTraceProjectionWriter,
)
from omnimarket.projection.runner import MessageMeta

_CHAIN = json.loads(
    (
        Path(__file__).resolve().parents[3] / "fixtures/omn19987/run_step_chain.json"
    ).read_text(encoding="utf-8")
)
_STEP: dict[str, Any] = _CHAIN[1]
_COLUMNS = (
    "tenant_id",
    "correlation_id",
    "step_sequence",
    "step_kind",
    "started_at",
    "duration_ms",
    "outcome",
    "failure_cause",
    "backend_id",
    "model_id",
    "attempt_number",
    "reason",
    "candidates",
    "failing_step_sequence",
)


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _Connection:
    """Keeps rows by key, the way the table's primary key does."""

    def __init__(self, rows: dict[tuple[str, str, int], dict[str, Any]]) -> None:
        self._rows = rows

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def execute(self, *_args: object) -> None:
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        key = (str(args[0]), str(args[1]), int(args[2]))
        if "INSERT INTO public.run_trace" in sql:
            if key in self._rows:
                return []
            row = dict(zip(_COLUMNS, args, strict=True))
            row["updated_at"] = datetime(2026, 10, 13, tzinfo=UTC)
            row["projection_cursor"] = len(self._rows) + 1
            self._rows[key] = row
            return [dict(row)]
        assert sql.lstrip().upper().startswith("SELECT"), sql
        return [dict(self._rows[key])] if key in self._rows else []


class _Acquire:
    def __init__(self, conn: _Connection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Connection:
        return self._conn

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _Pool:
    def __init__(self, conn: _Connection) -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


class _Db:
    def __init__(self, conn: _Connection) -> None:
        self.pool = _Pool(conn)


class _Published:
    """Records each delta; fails the first ``fail_first`` publishes."""

    def __init__(self, fail_first: int = 0) -> None:
        self.rows: list[dict[str, Any]] = []
        self.attempts = 0
        self._fail_first = fail_first

    async def __call__(self, _exposure: object, **kwargs: Any) -> bool:
        self.attempts += 1
        if self.attempts <= self._fail_first:
            raise ConnectionError("broker unavailable")
        self.rows.append(kwargs["row"])
        return True


def _writer(
    rows: dict[tuple[str, str, int], dict[str, Any]], published: _Published
) -> RunTraceProjectionWriter:
    stand_in = _Db(_Connection(rows))

    class _Writer(RunTraceProjectionWriter):
        @property
        def db(self) -> Any:
            return stand_in

        async def publish_snapshot_delta(self, exposure: Any, **kwargs: Any) -> bool:
            return await published(exposure, **kwargs)

    writer = _Writer()
    assert writer._snapshot_exposure is not None, "run-trace.v1 must be bus-backed"
    return writer


_META = MessageMeta(
    partition=0,
    offset=7,
    fallback_id="run-step-0-7",
    topic="onex.evt.omnimarket.delegation-run-step.v1",
)


async def test_a_failed_publish_is_raised_so_the_event_is_delivered_again() -> None:
    rows: dict[tuple[str, str, int], dict[str, Any]] = {}
    published = _Published(fail_first=1)
    writer = _writer(rows, published)

    with pytest.raises(ConnectionError):
        await writer._project_step(dict(_STEP), _META)

    assert len(rows) == 1, "the step is durable even though its snapshot did not go out"
    assert published.rows == []


async def test_the_redelivery_publishes_the_step_the_failed_publish_left_out() -> None:
    rows: dict[tuple[str, str, int], dict[str, Any]] = {}
    published = _Published(fail_first=1)
    writer = _writer(rows, published)
    with pytest.raises(ConnectionError):
        await writer._project_step(dict(_STEP), _META)

    again = await writer._project_step(dict(_STEP), _META)

    assert again is None, "a redelivery stores no new row"
    assert len(rows) == 1
    assert len(published.rows) == 1, (
        "the stored step reaches the snapshot on redelivery"
    )
    sent = published.rows[0]
    assert (sent["tenant_id"], sent["correlation_id"], sent["step_sequence"]) == (
        _STEP["tenant_id"],
        _STEP["correlation_id"],
        _STEP["step_sequence"],
    )
    assert sent["candidates"] == _STEP["candidates"]


async def test_a_changed_redelivery_publishes_the_stored_fact_not_its_own_content() -> (
    None
):
    rows: dict[tuple[str, str, int], dict[str, Any]] = {}
    published = _Published()
    writer = _writer(rows, published)
    await writer._project_step(dict(_STEP), _META)
    changed = {**_STEP, "reason": "a different reason", "backend_id": "another-backend"}

    await writer._project_step(changed, _META)

    assert len(rows) == 1
    assert [row["reason"] for row in published.rows] == [
        _STEP["reason"],
        _STEP["reason"],
    ]
    assert {row["backend_id"] for row in published.rows} == {_STEP["backend_id"]}


async def test_a_first_delivery_publishes_once_and_returns_the_row() -> None:
    rows: dict[tuple[str, str, int], dict[str, Any]] = {}
    published = _Published()
    writer = _writer(rows, published)

    row = await writer._project_step(dict(_STEP), _META)

    assert row is not None
    assert row["step_sequence"] == _STEP["step_sequence"]
    assert published.attempts == 1
    assert published.rows == [row]
