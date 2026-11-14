# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The usage-by-model-day writer folds delegation-completed events (OMN-22478).

On the dev bus onex.evt.omniintelligence.llm-call-completed.v1 holds no records: delegation calls the model itself
and reports the run on onex.evt.omnibase-infra.delegation-completed.v1, so usage_by_model_day stayed empty while
delegation_events held its runs and the Usage page read no tokens per day. The writer reads the delegation terminal
too; the call is the run, keyed by its correlation id and dated by the event's own envelope timestamp.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.events.topics import (
    DELEGATION_COMPLETED_TOPIC_V1,
    TOPIC_LLM_CALL_COMPLETED,
)
from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_usage_by_model_day_writer import (
    UsageByModelDayProjectionWriter,
    usage_call_from_delegation_completed,
)
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.unit

CORRELATION_ID = "5d987c21-e489-463d-8af7-451c13d076a1"


def _delegation_completed() -> dict[str, object]:
    """The payload shape read from the dev bus on 2026-11-08, with the runtime seam's injected keys."""
    return {
        "correlation_id": CORRELATION_ID,
        "task_type": "code_generation",
        "model_used": "Qwen3.8-27B",
        "prompt_tokens": 242,
        "completion_tokens": 43,
        "total_tokens": 285,
        "cumulative_attempt_cost": 0.0125,
        "final_attempt_cost": 0.0125,
        "cost_tier_name": "local",
        "tenant_id": "tenant-1",
        "_envelope_timestamp": "2026-11-08T07:41:13.320Z",
        "_envelope_id": "env-1",
        "_tenant_id": "tenant-1",
        "_db": object(),
    }


def test_the_contract_subscribes_to_the_delegation_terminal_beside_the_llm_call() -> (
    None
):
    from pathlib import Path

    contract = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1]
            / "src/omnimarket/nodes/node_projection_usage_by_model_day/contract.yaml"
        ).read_text()
    )
    assert contract["event_bus"]["subscribe_topics"] == [
        TOPIC_LLM_CALL_COMPLETED,
        DELEGATION_COMPLETED_TOPIC_V1,
    ]


def test_golden_a_delegation_terminal_becomes_one_dated_usage_call() -> None:
    event = usage_call_from_delegation_completed(_delegation_completed())
    assert event.call_id == CORRELATION_ID
    assert event.model_name == "Qwen3.8-27B"
    assert (event.prompt_tokens, event.completion_tokens) == (242, 43)
    assert event.estimated_cost_usd == pytest.approx(0.0125)
    assert event.tenant_id == "tenant-1"
    assert event.cost_tier_name == "local"
    # The terminal never says how its cost was obtained: unknown, never assumed measured.
    assert event.usage_source is EnumUsageSource.UNKNOWN
    assert event.timestamp == datetime(2026, 11, 8, 7, 41, 13, 320000, tzinfo=UTC)


def test_error_a_terminal_that_names_no_model_is_refused_not_booked_under_nothing() -> (
    None
):
    payload = _delegation_completed()
    payload.pop("model_used")
    with pytest.raises(ValidationError):
        usage_call_from_delegation_completed(payload)


def test_error_a_terminal_with_no_event_time_is_refused_not_dated_by_the_clock() -> (
    None
):
    payload = _delegation_completed()
    payload.pop("_envelope_timestamp")
    with pytest.raises(ValidationError):
        usage_call_from_delegation_completed(payload)


def test_a_terminal_without_cost_or_tier_books_zero_cost_and_not_tier_routed() -> None:
    payload = _delegation_completed()
    payload["cumulative_attempt_cost"] = None
    payload["cost_tier_name"] = None
    event = usage_call_from_delegation_completed(payload)
    assert event.estimated_cost_usd == 0.0
    assert event.cost_tier_name == ""


@pytest.fixture
def writer_boundary() -> tuple[UsageByModelDayProjectionWriter, MagicMock]:
    writer = UsageByModelDayProjectionWriter()
    db = MagicMock()
    conn = MagicMock()
    db.pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    conn.execute = AsyncMock()
    stored = {
        "tenant_id": "tenant-1",
        "usage_day": date(2026, 11, 8),
        "model_id": "Qwen3.8-27B",
        "cost_tier_name": "not_tier_routed",
        "input_tokens": 242,
        "output_tokens": 43,
        "cost_usd": Decimal("0.01250000"),
        "call_count": 1,
        "call_share": 1.0,
        "updated_at": datetime(2026, 11, 8, 8, tzinfo=UTC),
        "projection_cursor": 1,
    }
    conn.fetch = AsyncMock(side_effect=[[{"call_id": CORRELATION_ID}], [stored]])
    conn.fetchval = AsyncMock(return_value=False)
    writer._db = db
    writer.publish_snapshot_delta = AsyncMock()
    return writer, conn


async def test_the_writer_stores_a_delegation_terminal_as_a_call(
    writer_boundary: tuple[UsageByModelDayProjectionWriter, MagicMock],
) -> None:
    writer, conn = writer_boundary
    meta = MessageMeta(
        topic=DELEGATION_COMPLETED_TOPIC_V1, partition=0, offset=7, fallback_id="f"
    )
    assert await writer.project_event(
        DELEGATION_COMPLETED_TOPIC_V1, _delegation_completed(), meta
    )
    bound = conn.fetch.await_args_list[0].args
    assert bound[1:] == (
        CORRELATION_ID,
        "tenant-1",
        date(2026, 11, 8),
        "Qwen3.8-27B",
        242,
        43,
        Decimal("0.0125"),
        datetime(2026, 11, 8, 7, 41, 13, 320000, tzinfo=UTC),
        "local",
        "unknown",
    )


async def test_the_llm_call_topic_still_reads_the_llm_call_shape(
    writer_boundary: tuple[UsageByModelDayProjectionWriter, MagicMock],
) -> None:
    writer, conn = writer_boundary
    meta = MessageMeta(
        topic=TOPIC_LLM_CALL_COMPLETED, partition=0, offset=1, fallback_id="f"
    )
    payload = {
        "input_hash": "call-1",
        "model_id": "model-1",
        "tenant_id": "tenant-1",
        "created_at": "2026-09-29T03:30:00Z",
        "input_tokens": 12,
        "output_tokens": 3,
        "cost_usd": 0.001,
    }
    conn.fetch.side_effect = [
        [{"call_id": "call-1"}],
        [
            {
                "tenant_id": "tenant-1",
                "usage_day": date(2026, 9, 29),
                "model_id": "model-1",
                "cost_tier_name": "not_tier_routed",
                "input_tokens": 12,
                "output_tokens": 3,
                "cost_usd": Decimal("0.001"),
                "call_count": 1,
                "call_share": 1.0,
                "updated_at": datetime(2026, 9, 30, tzinfo=UTC),
                "projection_cursor": 2,
            }
        ],
    ]
    assert await writer.project_event(TOPIC_LLM_CALL_COMPLETED, payload, meta)
    assert conn.fetch.await_args_list[0].args[1] == "call-1"
