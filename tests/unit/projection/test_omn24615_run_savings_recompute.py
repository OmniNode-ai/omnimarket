# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24615 AC1: a local store's per-run saving is recomputed once, so every page reads one figure.

The savings view sums each run's stored modelled_savings_usd. A store written by an earlier build holds figures the
current definition would not give: a refused run with a positive figure, a run on a model with no price with a
figure, and a run from before the column existed with none. The headline is folded live with the current definition,
so on an operator store the two read 25.100618 and 37.636228. A one-time store step recomputes the derived columns
with the writer's own function. The recorded cost columns are never rewritten (decision D1).

Failure modes, each with a test below:
  R1  a stale stored figure keeps the savings view apart from the headline
  R2  the recorded cost columns are rewritten
  R3  the step runs again on every open, rewriting figures a later write set
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
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

_STEP = "omn24615_delegation_events_run_savings_recompute"
_NOW = datetime(2026, 11, 16, tzinfo=UTC)


def _stale_store(tmp_path: Path) -> Path:
    runs: tuple[dict[str, Any], ...] = (
        _delegation(
            "succeeded",
            model_name="gpt-4o-mini",
            delegated_to="gpt-4o-mini",
            cost_usd=0.0001,
        ),
        _delegation(
            "refused",
            model_name="gpt-4o-mini",
            delegated_to="gpt-4o-mini",
            cost_usd=0.0001,
            terminal_failure_cause="quality_gate_refused",
        ),
        _delegation(
            "unpriced",
            model_name="Beam-1.0-501B-A23B",
            delegated_to="Beam-1.0-501B-A23B",
            cost_usd=0.0,
        ),
        _delegation(
            "old", model_name="gpt-4o-mini", delegated_to="gpt-4o-mini", cost_usd=0.0001
        ),
    )
    path = _store(
        tmp_path, runs, tuple(_call(str(r["correlation_id"]), "measured") for r in runs)
    )
    with sqlite3.connect(path) as conn:
        # What an earlier build stored: a figure for the refused and the unpriced run, none for the old one.
        conn.execute(
            "UPDATE delegation_events SET modelled_savings_usd = '0.5', savings_measurement = 'measured' "
            "WHERE correlation_id IN ('refused', 'unpriced')"
        )
        conn.execute(
            "UPDATE delegation_events SET modelled_savings_usd = NULL, savings_baseline_model = NULL, "
            "savings_pricing_manifest_version = NULL, savings_measurement = NULL WHERE correlation_id = 'old'"
        )
        conn.execute(
            "DELETE FROM omnimarket_sqlite_store_steps WHERE step = ?", (_STEP,)
        )
    return path


def _stored(path: Path) -> dict[str, dict[str, Any]]:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return {
            str(r["correlation_id"]): dict(r)
            for r in conn.execute(
                "SELECT correlation_id, modelled_savings_usd, savings_measurement, cost_usd, cost_savings_usd "
                "FROM delegation_events"
            )
        }


def test_recompute_the_view_and_the_headline_read_one_figure(tmp_path: Path) -> None:
    """R1."""
    path = _stale_store(tmp_path)
    SqliteDatabaseAdapter(path)._connect().close()
    stored = _stored(path)
    assert stored["unpriced"]["modelled_savings_usd"] is None
    assert stored["unpriced"]["savings_measurement"] == "unknown_spend"
    assert float(stored["refused"]["modelled_savings_usd"]) == 0.0
    assert stored["old"]["modelled_savings_usd"] is not None
    rows = refresh_metering_summary(
        path, _TENANT, resolve_metering_baseline_model().model, _NOW
    )
    [headline] = [r for r in rows if r.window_kind == "all"]
    assert headline.savings_usd is not None
    assert _row(path)["cumulative_savings_usd"] == pytest.approx(
        float(headline.savings_usd)
    )


def test_recompute_leaves_the_recorded_cost_alone(tmp_path: Path) -> None:
    """R2."""
    path = _stale_store(tmp_path)
    before = {
        cid: (r["cost_usd"], r["cost_savings_usd"]) for cid, r in _stored(path).items()
    }
    SqliteDatabaseAdapter(path)._connect().close()
    after = {
        cid: (r["cost_usd"], r["cost_savings_usd"]) for cid, r in _stored(path).items()
    }
    assert after == before


def test_recompute_runs_once(tmp_path: Path) -> None:
    """R3."""
    path = _stale_store(tmp_path)
    SqliteDatabaseAdapter(path)._connect().close()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE delegation_events SET modelled_savings_usd = '0.25' WHERE correlation_id = 'old'"
        )
    SqliteDatabaseAdapter(path)._connect().close()
    assert _stored(path)["old"]["modelled_savings_usd"] == "0.25"
