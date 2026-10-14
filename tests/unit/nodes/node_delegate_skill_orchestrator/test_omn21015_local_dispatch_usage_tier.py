# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21015 AC4: the local delegate path books each usage call under the tier that served it.

Failure modes:

* the usage call carries no tier, so every local run lands under not_tier_routed;
* an escalated run's calls all carry the run's final tier, so a local model is shown under a cloud tier;
* the deciding call's tier differs from the tier the delegation row records for the run;
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


def test_an_escalated_runs_calls_each_stand_under_their_own_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, _, result = _ladder_run(
        tmp_path, monkeypatch, _MeteredTierEffect(pass_tiers=frozenset({"cheap_cloud"}))
    )
    assert result["status"] == "completed"

    calls = _rows(
        db_path,
        "SELECT model_id, cost_tier_name FROM usage_by_model_day_calls ORDER BY call_id",
    )
    assert [(c["model_id"], c["cost_tier_name"]) for c in calls] == [
        ("Qwen3.6-35B-A3B", "local"),
        ("glm-5.2", "cheap_cloud"),
    ]
    # The deciding call's tier is the run's, as the delegation row records it.
    (delegation,) = _rows(db_path, "SELECT cost_tier_name FROM delegation_events")
    assert delegation["cost_tier_name"] == calls[-1]["cost_tier_name"]

    shares = {
        (r["model_id"], r["cost_tier_name"]): r["call_share"]
        for r in _rows(db_path, "SELECT * FROM usage_by_model_day")
    }
    assert shares == {
        ("Qwen3.6-35B-A3B", "local"): pytest.approx(0.5),
        ("glm-5.2", "cheap_cloud"): pytest.approx(0.5),
    }
