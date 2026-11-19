# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-25863: a store recomputes its stored per-run savings whenever the savings rule or the price manifest changes.

Each local store keeps a per-run modelled saving that the savings view sums, computed once per store by the one-time
step omn24615_delegation_events_run_savings_recompute. omnimarket#6442 changed the rule (a run is priced by the model
that ran, model_name before delegated_to) with no new recompute, so a store that had recorded the step kept savings
priced by the alias, while the metering headline, folded live, used the new rule. A price manifest change leaves the
same gap. The recompute now runs as a store step named by a digest of the rule's version and every manifest price.

Failure modes, each with a test below:
  R1  a store that recorded the earlier recompute keeps a saving priced by the alias (the ticket's falsifier)
  R2  a price manifest change leaves stored savings at the old prices
  R3  the recompute runs on every open instead of once per change
  R4  the recorded cost columns are rewritten
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models import model_local_install_schema as schema
from omnimarket.nodes.node_projection_delegation.handlers import handler_run_savings
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from tests.unit.projection.test_sqlite_delegation_savings_view_omn20008 import (
    _delegation,
    _store,
)

pytestmark = pytest.mark.unit

_STALE = "9.999"


def _open(path: Path) -> None:
    SqliteDatabaseAdapter(path)._connect().close()


def _saving_and_cost(path: Path) -> tuple[Any, Any]:
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT modelled_savings_usd, cost_usd FROM delegation_events WHERE correlation_id = 'aliased'"
        ).fetchone()
    return row[0], row[1]


def _expected(path: Path) -> Any:
    """The saving the current rule gives this run, from the writer's own function."""
    columns = ["correlation_id", *handler_run_savings.RUN_SAVINGS_INPUT_COLUMNS]
    with sqlite3.connect(path) as conn:
        values = conn.execute(
            f"SELECT {', '.join(columns)} FROM delegation_events WHERE correlation_id = 'aliased'"
        ).fetchone()
    return handler_run_savings.modelled_run_savings_columns(
        dict(zip(columns, values, strict=True))
    )["modelled_savings_usd"]


def _earlier_store(tmp_path: Path) -> Path:
    """A store an earlier build wrote: the run priced by its alias, and no rule-digest step recorded."""
    path = _store(
        tmp_path,
        (
            _delegation(
                "aliased",
                model_name="gpt-4o-mini",
                delegated_to="gpt-4o",
                cost_usd=0.0001,
                tokens_input=2000,
                tokens_output=500,
            ),
        ),
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE delegation_events SET modelled_savings_usd = ? WHERE correlation_id = 'aliased'",
            (_STALE,),
        )
        conn.execute(
            "DELETE FROM omnimarket_sqlite_store_steps WHERE step LIKE 'omn25863_%'"
        )
    return path


def test_recompute_savings_a_store_with_the_earlier_recompute_reprices(
    tmp_path: Path,
) -> None:
    """R1: the ticket's falsifier."""
    path = _earlier_store(tmp_path)
    _open(path)
    saving, _cost = _saving_and_cost(path)
    assert saving != _STALE
    assert saving == _expected(path)


def test_recompute_savings_a_price_manifest_change_recomputes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R2: a changed manifest names a new step, so the store recomputes once more."""
    path = _earlier_store(tmp_path)
    _open(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE delegation_events SET modelled_savings_usd = ? WHERE correlation_id = 'aliased'",
            (_STALE,),
        )
    real = schema._manifest_price_lines
    monkeypatch.setattr(
        schema, "_manifest_price_lines", lambda: [*real(), "a-new-model,0.001,0.002"]
    )
    _open(path)
    assert _saving_and_cost(path)[0] == _expected(path)


def test_recompute_savings_runs_once_per_change(tmp_path: Path) -> None:
    """R3: with nothing changed, a second open recomputes nothing."""
    path = _earlier_store(tmp_path)
    _open(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE delegation_events SET modelled_savings_usd = ? WHERE correlation_id = 'aliased'",
            (_STALE,),
        )
    _open(path)
    assert _saving_and_cost(path)[0] == _STALE


def test_recompute_savings_leaves_recorded_cost_alone(tmp_path: Path) -> None:
    """R4: decision D1 of OMN-24615, recorded cost columns are never rewritten."""
    path = _earlier_store(tmp_path)
    _open(path)
    assert _saving_and_cost(path)[1] == pytest.approx(0.0001)
