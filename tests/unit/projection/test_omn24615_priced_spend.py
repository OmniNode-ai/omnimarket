# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24615 AC3 (decisions D1, D3): spend comes only from a named price source.

The audit found all $0.232022 of stored spend was a routing tier's flat rate on
36 runs, $0.152610 of it on ':free' models, and Beam runs booked at 0.0 with no
price at all. A run's recorded cost is now spend only when the pricing manifest
prices its model, and not a positive cost on a model the manifest lists as
free. Any other run is unpriced: counted in runs_unknown_spend, in no sum, and
given no modelled saving. Stored rows are not rewritten (D1).

Failure modes, each with a test below:
  P1  a ':free' model's tier-rate cost is summed as spend
  P2  a model with no price is booked as a measured, free run
  P3  the local model the manifest prices at zero loses its measured zero
  P4  an unpriced run is still given a modelled saving, so the view disagrees
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_run_savings import (
    modelled_run_savings_columns,
)
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
_FREE = "nvidia/nemotron-3-ultra-550b-a55b:free"
_UNPRICED = "Beam-1.0-501B-A23B"


def _run(cid: str, model: str, cost: float) -> dict[str, object]:
    return _delegation(cid, model_name=model, delegated_to=model, cost_usd=cost)


def _tier_rate_run() -> dict[str, object]:
    return {**_run("free-tier", _FREE, 0.00104), "cost_tier_name": "cheap_cloud"}


def test_priced_spend_only_named_prices_are_summed(tmp_path: Path) -> None:
    """P1, P2, P3: the AC3 falsifier."""
    runs = (
        _run("local", "Qwen3.8-27B", 0.0),
        _run("paid", "gpt-4o", 0.01),
        _tier_rate_run(),
        _run("unpriced", _UNPRICED, 0.0),
    )
    path = _store(
        tmp_path, runs, tuple(_call(str(r["correlation_id"]), "measured") for r in runs)
    )
    rows = refresh_metering_summary(
        path, _TENANT, resolve_metering_baseline_model().model, _NOW
    )
    [row] = [r for r in rows if r.window_kind == "all"]
    assert row.spend_usd is not None
    assert Decimal(row.spend_usd) == Decimal("0.01")
    assert row.runs_measured == 2
    assert row.runs_unknown_spend == 2


def test_priced_spend_an_unpriced_run_has_no_modelled_saving() -> None:
    """P4."""
    for row in (_tier_rate_run(), _run("unpriced", _UNPRICED, 0.0)):
        columns = modelled_run_savings_columns(row)
        assert columns["modelled_savings_usd"] is None
        assert columns["savings_measurement"] == "unknown_spend"
    priced = modelled_run_savings_columns(_run("local", "Qwen3.8-27B", 0.0))
    assert priced["modelled_savings_usd"] is not None
