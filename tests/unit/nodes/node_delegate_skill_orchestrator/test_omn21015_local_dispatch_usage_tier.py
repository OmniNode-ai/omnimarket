# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21015 AC4: the local delegate path books each usage call under the run's tier.

The ruling (ticket comment af693058, 2026-10-14T04:50Z): "the usage call event should carry the run's
tier so the tier joins the row key". The run's tier is the one its delegation row records (OMN-20755).

Failure modes:

* the usage call carries no tier, so every local run lands under not_tier_routed;
* an escalated run's earlier call carries the tier of the backend that served that attempt, so one run
  is split across two tiers;
* a call's tier differs from the tier the delegation row records for the run;
* the day's shares do not cover the run's calls.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tests.unit.nodes.node_delegate_skill_orchestrator.test_local_dispatch_usage_by_model_day import (
    _ladder_run,
    _MeteredTierEffect,
)


def _rows(db_path: Path, sql: str) -> list[sqlite3.Row]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(sql).fetchall()


def test_an_escalated_runs_calls_all_stand_under_the_runs_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, _, result = _ladder_run(
        tmp_path, monkeypatch, _MeteredTierEffect(pass_tiers=frozenset({"cheap_cloud"}))
    )
    assert result["status"] == "completed"

    (delegation,) = _rows(db_path, "SELECT cost_tier_name FROM delegation_events")
    assert delegation["cost_tier_name"] == "cheap_cloud"

    calls = _rows(
        db_path,
        "SELECT model_id, cost_tier_name FROM usage_by_model_day_calls ORDER BY call_id",
    )
    # Each call keeps the model that served it, under the run's tier.
    assert [(c["model_id"], c["cost_tier_name"]) for c in calls] == [
        ("Qwen3.6-35B-A3B", "cheap_cloud"),
        ("glm-5.2", "cheap_cloud"),
    ]

    shares = {
        (r["model_id"], r["cost_tier_name"]): r["call_share"]
        for r in _rows(db_path, "SELECT * FROM usage_by_model_day")
    }
    assert shares == {
        ("Qwen3.6-35B-A3B", "cheap_cloud"): pytest.approx(0.5),
        ("glm-5.2", "cheap_cloud"): pytest.approx(0.5),
    }
