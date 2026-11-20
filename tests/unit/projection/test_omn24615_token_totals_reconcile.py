# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24615 AC5: metering-summary.v1 and usage-by-model-day.v1 serve the same token totals.

The audit found Usage showing 8,589,587 input tokens from metering-summary.v1 and 4,194,834 from
usage-by-model-day.v1: metering summed the tokens of runs with no LLM call record, which usage-by-model-day never
sees. A run with no call record now has unknown tokens in metering too. A run whose calls were recorded but not
measured by the provider keeps its tokens in both totals and is left out of the saving only.

Failure modes, each with a test below:
  T1  metering sums the tokens of a run with no call record
  T2  metering drops the tokens of a run whose recorded calls were not provider-measured
  T3  a zero-token, no-cost call is counted as a measured call
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

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
from omnimarket.projection.sqlite_metering_summary import (
    refresh_metering_summary,
    resolve_metering_baseline_model,
)
from tests.unit.projection.test_sqlite_delegation_savings_view_omn20008 import (
    _TENANT,
    _call,
    _delegation,
    _store,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 11, 16, tzinfo=UTC)


def _usage_call(
    call_id: str, tokens_in: int, tokens_out: int, source: str
) -> ModelUsageCallEvent:
    return ModelUsageCallEvent.model_validate(
        {
            "call_id": call_id,
            "model_name": "gpt-4o-mini",
            "timestamp": "2026-10-10T18:13:00Z",
            "prompt_tokens": tokens_in,
            "completion_tokens": tokens_out,
            "estimated_cost_usd": 0.0 if not (tokens_in or tokens_out) else 0.0001,
            "tenant_id": _TENANT,
            "usage_source": source,
        }
    )


def _runs(tmp_path: Path) -> Path:
    runs: tuple[dict[str, Any], ...] = (
        _delegation(
            "measured",
            model_name="gpt-4o-mini",
            delegated_to="gpt-4o-mini",
            cost_usd=0.0001,
        ),
        _delegation(
            "unknown-usage",
            model_name="gpt-4o-mini",
            delegated_to="gpt-4o-mini",
            cost_usd=0.0001,
            tokens_input=100,
            tokens_output=10,
        ),
        _delegation(
            "no-call",
            model_name="gpt-4o-mini",
            delegated_to="gpt-4o-mini",
            cost_usd=0.0001,
            tokens_input=50,
            tokens_output=5,
        ),
    )
    path = _store(
        tmp_path,
        runs,
        (_call("measured", "measured"), _call("unknown-usage", "unknown")),
    )
    db = SqliteDatabaseAdapter(path)
    fold = HandlerProjectionUsageByModelDay()
    for event in (
        _usage_call("measured", 213, 6, "measured"),
        _usage_call("unknown-usage", 100, 10, "unknown"),
        _usage_call("empty", 0, 0, "measured"),
    ):
        apply_usage_call(fold.handle(event), db)
    return path


def test_token_totals_reconcile_between_metering_and_usage(tmp_path: Path) -> None:
    """T1, T2, T3: the AC5 falsifier."""
    path = _runs(tmp_path)
    rows = refresh_metering_summary(
        path, _TENANT, resolve_metering_baseline_model().model, _NOW
    )
    [metering] = [r for r in rows if r.window_kind == "all"]
    usage = SqliteDatabaseAdapter(path).query(AGGREGATE_TABLE, {"tenant_id": _TENANT})
    usage_in = sum(int(str(r["input_tokens"])) for r in usage)
    usage_out = sum(int(str(r["output_tokens"])) for r in usage)
    assert (
        (metering.tokens_in, metering.tokens_out) == (usage_in, usage_out) == (313, 16)
    )
    assert sum(int(str(r["unmeasured_call_count"])) for r in usage) == 2
    assert metering.runs_unknown_tokens == 2
    assert metering.runs_measured == 1
    assert metering.savings_usd is not None
    assert Decimal(metering.savings_usd) > 0
