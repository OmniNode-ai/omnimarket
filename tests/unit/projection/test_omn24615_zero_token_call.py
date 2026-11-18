# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24615 AC5: a call with 0 input and 0 output tokens and no cost is not a measured call.

The audit found usage-by-model-day.v1 counted 755 calls with 0 input and 0
output tokens as measured. A call that recorded no tokens and no cost recorded
nothing, so the fold now classes it unknown: it counts in
unmeasured_call_count and in no measured cost, on either store. An escalated
run books each attempt that did not decide it with 0/0 tokens (tokens follow
metering's deciding attempt) and its own measured cost, so the run's cost split
adds up to its total; such a call keeps its measured cost (decision 3).

Failure modes, each with a test below:
  Z1  a 0/0 call recorded as measured is counted as measured
  Z2  a measured call with tokens is no longer counted as measured
  Z3  a call with tokens in one direction only is read as recording nothing
  Z4  an escalation attempt booked 0/0 with a measured cost loses that cost
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_projection_usage_by_model_day import (
    HandlerProjectionUsageByModelDay,
)
from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_usage_by_model_day_store import (
    AGGREGATE_TABLE,
    apply_usage_call,
)
from omnimarket.nodes.node_projection_usage_by_model_day.models.model_usage_call_event import (
    ModelUsageCallEvent,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit


def _call(
    call_id: str, tokens_in: int, tokens_out: int, cost: float
) -> ModelUsageCallEvent:
    return ModelUsageCallEvent.model_validate(
        {
            "call_id": call_id,
            "model_name": "m",
            "timestamp": "2026-09-28T00:00:00Z",
            "prompt_tokens": tokens_in,
            "completion_tokens": tokens_out,
            "estimated_cost_usd": cost,
            "tenant_id": "omninode",
            "usage_source": "measured",
        }
    )


def test_zero_token_call_is_not_a_measured_call(tmp_path: Path) -> None:
    """Z1, Z2: the AC5 falsifier's zero-token half."""
    db = SqliteDatabaseAdapter(tmp_path / "usage.sqlite")
    fold = HandlerProjectionUsageByModelDay()
    for event in (_call("real", 10, 2, 0.25), _call("empty", 0, 0, 0.0)):
        apply_usage_call(fold.handle(event), db)
    (row,) = db.query(AGGREGATE_TABLE)
    assert Decimal(str(row["measured_cost_usd"])) == Decimal("0.25")
    assert int(str(row["unmeasured_call_count"])) == 1
    assert (int(str(row["input_tokens"])), int(str(row["output_tokens"]))) == (10, 2)
    assert int(str(row["call_count"])) == 2


@pytest.mark.parametrize(("tokens_in", "tokens_out"), [(10, 0), (0, 3)])
def test_zero_token_call_one_direction_is_still_measured(
    tokens_in: int, tokens_out: int
) -> None:
    """Z3."""
    delta = HandlerProjectionUsageByModelDay().handle(
        _call("one-way", tokens_in, tokens_out, 0.1)
    )
    assert delta.usage_source is EnumUsageSource.MEASURED


def test_zero_token_call_an_escalation_attempt_keeps_its_cost() -> None:
    """Z4."""
    delta = HandlerProjectionUsageByModelDay().handle(_call("attempt", 0, 0, 0.001))
    assert delta.usage_source is EnumUsageSource.MEASURED
