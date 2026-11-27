# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-25861: the Postgres writer follows each of migration 0004's two columns as it finds it.

0004 is two ALTER TABLE statements that the forward-migration runner applies without one wrapping transaction, so a
database can hold the calls table's backend column before the day table's backends column. The writer reads each
column on its own, on the connection the write runs on; these cases drive the writer's own _project_call through a
connection whose catalogue answers as such a database would, and read the statements and arguments it sends.
tests/test_omn25861_usage_backends_real_postgres.py runs the same statements on a real server.

Failure modes:
W1  with only the calls column in force, a stated backend is dropped from the stored call (its row would then serve
    NULL on every later recount), or the writer sends a statement naming the day column that is not there;
W2  with both columns in force, the recount does not write the row's backends, the reshare does not return them, or
    the row the writer returns carries them as JSON text instead of the list the exposure serves;
W3  with neither column in force, the writer names either column.
"""

from datetime import date
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_usage_by_model_day_writer import (
    UsageByModelDayProjectionWriter,
)
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.unit


def _payload() -> dict[str, object]:
    return {
        "call_id": "call-1",
        "model_name": "Beam-1.0-501B-A23B",
        "tenant_id": "tenant-1",
        "timestamp": "2026-11-19T05:00:00Z",
        "prompt_tokens": 12,
        "completion_tokens": 3,
        "estimated_cost_usd": 0.001,
        "cost_tier_name": "cheap_cloud",
        "backend": "byok-reflection",
    }


def _writer(
    *, calls_column: bool, day_column: bool, day_row: dict[str, Any]
) -> tuple[UsageByModelDayProjectionWriter, MagicMock]:
    writer = UsageByModelDayProjectionWriter()
    db = MagicMock()
    conn = MagicMock()
    db.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    conn.execute = AsyncMock()

    async def fetchval(sql: str, *_: object) -> bool:
        # The catalogue as a database at this point of 0004 answers; the tier key is not in force.
        if "attname = 'backends'" in sql:
            return day_column
        if "attname = 'backend'" in sql:
            return calls_column
        return False

    conn.fetchval = AsyncMock(side_effect=fetchval)
    conn.fetch = AsyncMock(side_effect=[[{"call_id": "call-1"}], [day_row]])
    writer._db = db
    writer.publish_snapshot_delta = AsyncMock()
    return writer, conn


def _row(**extra: object) -> dict[str, Any]:
    return {
        "tenant_id": "tenant-1",
        "usage_day": date(2026, 11, 19),
        "model_id": "Beam-1.0-501B-A23B",
        "cost_tier_name": "not_tier_routed",
        "call_count": 1,
        **extra,
    }


def _meta(writer: UsageByModelDayProjectionWriter) -> MessageMeta:
    return MessageMeta(topic=writer.topics[0], partition=0, offset=1, fallback_id="f")


def _sent(conn: MagicMock) -> tuple[str, tuple[object, ...], list[str]]:
    insert = conn.fetch.await_args_list[0].args
    executed = [str(call.args[0]) for call in conn.execute.await_args_list]
    return str(insert[0]), insert[1:], executed


async def test_w1_a_half_applied_0004_still_stores_the_stated_backend() -> None:
    writer, conn = _writer(calls_column=True, day_column=False, day_row=_row())
    row = await writer._project_call(_payload(), _meta(writer))
    insert, args, executed = _sent(conn)
    assert "backend" in insert
    assert args[-1] == "byok-reflection"
    assert not any("backends" in sql for sql in executed)
    assert "backends" not in str(conn.fetch.await_args_list[1].args[0])
    assert row is not None
    assert "backends" not in row


async def test_w2_with_both_columns_the_row_carries_its_backends_as_a_list() -> None:
    writer, conn = _writer(
        calls_column=True,
        day_column=True,
        day_row=_row(backends='["byok-reflection"]'),
    )
    row = await writer._project_call(_payload(), _meta(writer))
    _, args, executed = _sent(conn)
    assert args[-1] == "byok-reflection"
    assert any("backends = EXCLUDED.backends" in sql for sql in executed)
    assert "agg.backends" in str(conn.fetch.await_args_list[1].args[0])
    assert row is not None
    assert row["backends"] == ["byok-reflection"]
    published = writer.publish_snapshot_delta.await_args_list[0].kwargs["row"]
    assert published["backends"] == ["byok-reflection"]


async def test_w3_before_0004_the_writer_names_neither_column() -> None:
    writer, conn = _writer(calls_column=False, day_column=False, day_row=_row())
    await writer._project_call(_payload(), _meta(writer))
    insert, args, executed = _sent(conn)
    assert "backend" not in insert
    assert "byok-reflection" not in args
    assert not any("backend" in sql for sql in executed)
