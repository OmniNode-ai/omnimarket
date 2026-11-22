# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-25862: the metering headline and the local views count unpriced runs by one rule.

A cost counts as spend only from a named price (OMN-24615 AC3). The headline's rule is ``priced_spend``: a run on a
model the pricing manifest does not list is unpriced at any cost, $0 included, and gets no saving. The savings view
(revision 6) and model-routing view v4 counted such a $0 run as a measured zero, so for one store the headline served a
higher unpriced count than the savings and model-routing pages (found by the re-review of omnimarket#6442).

Failure modes, each with a test below:
  U1  a $0 run on a model the manifest does not list is unpriced in the headline but a measured zero in the views
  U2  over-correction: a $0 run on a model the manifest lists (free or priced) becomes unpriced
  U3  over-correction: a positive cost on a priced model stops being spend
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.model_projection_read import ModelProjectionReadRequest
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.sqlite_metering_summary import (
    refresh_metering_summary,
    resolve_metering_baseline_model,
)
from tests.unit.projection.test_omn24615_evaluated_runs_and_served_model import (
    _ROUTING,
    _TOPICS,
)
from tests.unit.projection.test_sqlite_delegation_savings_view_omn20008 import (
    _TENANT,
    _call,
    _delegation,
    _row,
    _store,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 11, 19, tzinfo=UTC)
_UNLISTED = "vendor/unlisted-model"


def _run(cid: str, model: str, cost: float) -> dict[str, Any]:
    return _delegation(cid, model_name=model, delegated_to=model, cost_usd=cost)


def _counts(path: Path) -> tuple[int, int, int]:
    """(headline runs_unknown_spend, savings unpriced_call_count, routing unmeasured_cost_count summed)."""
    rows = refresh_metering_summary(
        path, _TENANT, resolve_metering_baseline_model().model, _NOW
    )
    [headline] = [r for r in rows if r.window_kind == "all"]
    savings = _row(path)["unpriced_call_count"]
    handler = HandlerProjectionRead(
        topic_map=_TOPICS, row_source=SqliteTableRowSource(path)
    )
    result = asyncio.run(
        handler.handle(ModelProjectionReadRequest(topic=_ROUTING, tenant_id=_TENANT))
    )
    assert result.http_status == 200, result.response
    [routing] = (result.response or {})["rows"]
    unmeasured = sum(
        int(model["unmeasured_cost_count"])
        for tier in routing["by_tier"]["tiers"]
        for model in tier["models"]
    )
    return int(headline.runs_unknown_spend), int(savings), unmeasured


def _store_of(tmp_path: Path, *runs: dict[str, Any]) -> Path:
    return _store(
        tmp_path, runs, tuple(_call(str(r["correlation_id"]), "measured") for r in runs)
    )


def test_one_unpriced_rule_a_zero_cost_run_on_an_unlisted_model(tmp_path: Path) -> None:
    """U1: the falsifier. The three readings agree, and the unlisted $0 run is the one unpriced run."""
    path = _store_of(
        tmp_path,
        _run("unlisted-zero", _UNLISTED, 0.0),
        _run("free-listed-zero", "qwen2.5-7b", 0.0),
        _run("priced", "gpt-4o-mini", 0.0001),
    )
    assert _counts(path) == (1, 1, 1)


def test_one_unpriced_rule_listed_models_at_zero_stay_measured(tmp_path: Path) -> None:
    """U2 and U3: listed models at $0 and a priced positive cost are not unpriced anywhere."""
    path = _store_of(
        tmp_path,
        _run("free-listed-zero", "qwen2.5-7b", 0.0),
        _run("priced-zero", "gpt-4o-mini", 0.0),
        _run("priced", "gpt-4o-mini", 0.0001),
    )
    assert _counts(path) == (0, 0, 0)
