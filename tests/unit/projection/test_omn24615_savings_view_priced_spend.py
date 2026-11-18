# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24615 AC1 and AC3, follow-up: the savings view serves spend and savings by the metering headline's rules.

The independent review of 2026-11-18 found the one-definition savings view still apart from the headline:
the view's local spend summed the raw stored cost, so a routing tier's flat rate on a model with no price was
still spend there (AC3), and a store with no call record read every run as unknown usage, so the view served no
saving where the headline served one (AC1, "every local page reads that figure").

Failure modes, each with a test below:
  P1  a run on a model with no named price adds its flat-rate cost to the view's local spend, or is not counted
      as unpriced (AC3)
  P2  on a store that has recorded no call, the view's saving differs from the metering headline (AC1)
  P3  a priced run's spend is dropped (over-exclusion)
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.projection.sqlite_metering_summary import (
    refresh_metering_summary,
    resolve_metering_baseline_model,
)
from tests.unit.projection.test_sqlite_delegation_savings_view_omn20008 import (
    _TENANT,
    _call,
    _delegation,
    _row,
    _store,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 11, 16, tzinfo=UTC)


def _priced_and_flat_rate(tmp_path: Path) -> Path:
    runs: tuple[dict[str, Any], ...] = (
        _delegation(
            "priced",
            model_name="gpt-4o-mini",
            delegated_to="gpt-4o-mini",
            cost_usd=0.0001,
            savings_measurement="measured",
        ),
        _delegation(
            "flat-rate",
            model_name="vendor/model:free",
            delegated_to="vendor/model:free",
            cost_usd=0.05,
            savings_measurement="unknown_spend",
        ),
    )
    return _store(
        tmp_path, runs, tuple(_call(str(r["correlation_id"]), "measured") for r in runs)
    )


def test_savings_view_priced_spend_excludes_a_flat_rate_on_a_model_with_no_price(
    tmp_path: Path,
) -> None:
    """P1 and P3: the AC3 falsifier on the savings topic."""
    row = _row(_priced_and_flat_rate(tmp_path))
    assert row["cumulative_local_cost_usd"] == pytest.approx(0.0001)
    assert row["unpriced_call_count"] == 1


def test_savings_view_priced_spend_a_store_with_no_call_record_reads_the_headline(
    tmp_path: Path,
) -> None:
    """P2: the AC1 falsifier on a store whose call table is empty."""
    path = _store(tmp_path, (_delegation("measured"),))
    rows = refresh_metering_summary(
        path, _TENANT, resolve_metering_baseline_model().model, _NOW
    )
    [headline] = [r for r in rows if r.window_kind == "all"]
    assert headline.savings_usd is not None
    assert _row(path)["cumulative_savings_usd"] == pytest.approx(
        float(headline.savings_usd)
    )
