# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Writer boundary checks beyond the SQLite acceptance specification."""

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_projection_usage_by_model_day import (
    HandlerProjectionUsageByModelDay,
)
from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_usage_by_model_day_writer import (
    UsageByModelDayProjectionWriter,
)
from omnimarket.nodes.node_projection_usage_by_model_day.models import (
    ModelUsageCallEvent,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_isolation import TENANT_GUC

pytestmark = pytest.mark.unit


def _payload() -> dict[str, object]:
    return {
        "input_hash": "call-1",
        "model_id": "model-1",
        "tenant_id": "tenant-1",
        "created_at": "2026-09-28T23:30:00-04:00",
        "input_tokens": 12,
        "output_tokens": 3,
        "cost_usd": 0.001,
        "_db": object(),
        "_envelope_timestamp": "2000-01-01T00:00:00Z",
    }


@pytest.mark.parametrize("timestamp", [None, "", "not-a-date"])
def test_unusable_timestamp_is_a_validation_error(timestamp: object) -> None:
    with pytest.raises(ValidationError):
        ModelUsageCallEvent.model_validate({"model_id": "m", "emitted_at": timestamp})


def test_aliases_and_fallback_identity_use_canonical_utc_time() -> None:
    payload = _payload()
    payload.pop("input_hash")
    fold = HandlerProjectionUsageByModelDay()
    first = fold.handle(ModelUsageCallEvent.model_validate(payload))
    payload["created_at"] = "2026-09-29T03:30:00Z"
    assert fold.handle(ModelUsageCallEvent.model_validate(payload)) == first
    assert len(first.call_id) == 64
    assert first.cost_usd == Decimal("0.001")
    assert (first.input_tokens, first.output_tokens) == (12, 3)
    payload["tenant_id"] = "tenant-2"
    assert (
        fold.handle(ModelUsageCallEvent.model_validate(payload)).call_id
        != first.call_id
    )


@pytest.fixture
def writer_boundary() -> tuple[UsageByModelDayProjectionWriter, MagicMock, MagicMock]:
    writer = UsageByModelDayProjectionWriter()
    db = MagicMock()
    conn = MagicMock()
    db.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    conn.execute = AsyncMock()
    conn.fetch = AsyncMock()
    writer._db = db
    writer.publish_snapshot_delta = AsyncMock()
    return writer, db, conn


def _meta(writer: UsageByModelDayProjectionWriter) -> MessageMeta:
    return MessageMeta(topic=writer.topics[0], partition=2, offset=19, fallback_id="f")


async def test_publishes_database_row_only_after_transaction_commit(
    writer_boundary,
) -> None:
    writer, db, conn = writer_boundary
    stored = {
        "tenant_id": "tenant-1",
        "usage_day": date(2026, 9, 29),
        "model_id": "model-1",
        "input_tokens": 120,
        "output_tokens": 30,
        "cost_usd": Decimal("0.01000000"),
        "call_count": 10,
        "updated_at": datetime(2026, 9, 30, tzinfo=UTC),
        "projection_cursor": 42,
    }
    conn.fetch.side_effect = [[{"call_id": "call-1"}], [stored]]

    async def published(*args, **kwargs):
        conn.transaction.return_value.__aexit__.assert_awaited_once_with(
            None, None, None
        )
        assert kwargs["row"]["input_tokens"] == 120
        assert kwargs["row"]["cost_usd"] == "0.01000000"
        assert kwargs["row"]["usage_day"] == "2026-09-29"
        assert kwargs["tenant_id"] == "tenant-1"
        assert kwargs["source_event_id"] == "call-1"
        assert kwargs["source_partition"] == 2
        assert kwargs["source_offset"] == 19

    writer.publish_snapshot_delta.side_effect = published
    assert await writer.project_event(writer.topics[0], _payload(), _meta(writer))
    db.pool.acquire.assert_called_once()
    conn.transaction.assert_called_once()
    conn.execute.assert_any_await(
        "SELECT set_config($1, $2, true)", TENANT_GUC, "tenant-1"
    )
    bound = conn.fetch.await_args_list[0].args
    assert bound[1:] == (
        "call-1",
        "tenant-1",
        date(2026, 9, 29),
        "model-1",
        12,
        3,
        Decimal("0.001"),
        datetime(2026, 9, 29, 3, 30, tzinfo=UTC),
        # OMN-20006: a payload that never says how its cost was obtained is stored
        # as unknown, so the recount keeps it out of measured_cost_usd.
        "unknown",
    )
    writer.publish_snapshot_delta.assert_awaited_once()


async def test_the_usage_source_is_bound_on_insert(writer_boundary) -> None:
    writer, _, conn = writer_boundary
    conn.fetch.return_value = []
    payload = _payload()
    payload["usage_source"] = "API"
    await writer.project_event(writer.topics[0], payload, _meta(writer))
    assert conn.fetch.await_args_list[0].args[-1] == "measured"


def test_the_recount_sums_only_measured_cost_and_counts_the_rest() -> None:
    from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_usage_by_model_day_writer import (
        _RECOUNT_AGGREGATE,
    )

    sql = " ".join(_RECOUNT_AGGREGATE.split())
    assert "SUM(cost_usd) FILTER (WHERE usage_source = 'measured')" in sql
    assert "COUNT(*) FILTER (WHERE usage_source <> 'measured')" in sql
    assert "measured_cost_usd = EXCLUDED.measured_cost_usd" in sql
    assert "unmeasured_call_count = EXCLUDED.unmeasured_call_count" in sql


async def test_duplicate_skips_recount_and_snapshot(writer_boundary) -> None:
    writer, _, conn = writer_boundary
    conn.fetch.return_value = []
    assert await writer.project_event(writer.topics[0], _payload(), _meta(writer))
    conn.fetch.assert_awaited_once()
    writer.publish_snapshot_delta.assert_not_awaited()


async def test_failed_recount_aborts_transaction_and_publishes_nothing(
    writer_boundary,
) -> None:
    writer, _, conn = writer_boundary
    conn.fetch.side_effect = [[{"call_id": "call-1"}], RuntimeError("recount failed")]
    with pytest.raises(RuntimeError, match="recount failed"):
        await writer.project_event(writer.topics[0], _payload(), _meta(writer))
    assert conn.transaction.return_value.__aexit__.await_args.args[0] is RuntimeError
    writer.publish_snapshot_delta.assert_not_awaited()


async def test_missing_timestamp_propagates_before_database_access(
    writer_boundary,
) -> None:
    writer, db, _ = writer_boundary
    payload = _payload()
    payload.pop("created_at")
    with pytest.raises(ValidationError):
        await writer.project_event(writer.topics[0], payload, _meta(writer))
    db.pool.acquire.assert_not_called()
    writer.publish_snapshot_delta.assert_not_awaited()
