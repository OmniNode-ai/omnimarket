# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for OmniGate projection reducer."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_omnigate_projection.models.model_omnigate_projection_row import (
    ModelOmniGateMetricsSnapshot,
)
from omnimarket.nodes.node_omnigate_projection.reducers.reducer_omnigate_projection import (
    reduce_omnigate_projection,
)

pytestmark = pytest.mark.unit


def test_projection_reducer_builds_activity_and_metrics() -> None:
    activity, metrics = reduce_omnigate_projection(
        (),
        ModelOmniGateMetricsSnapshot(),
        {
            "repository_id": "123",
            "project_name": "Omni",
            "branch": "feature",
            "ok": False,
            "action": "fail",
            "reason": "Checks failed: lint",
            "receipt_diff_hash": "sha256:" + "c" * 64,
            "checked_at": "2026-05-17T12:00:00Z",
            "checks": [{"name": "lint", "status": "FAIL"}],
        },
    )

    assert len(activity) == 1
    assert activity[0].status == "fail"
    assert activity[0].failed_checks == 1
    assert activity[0].diff_hash == "sha256:" + "c" * 64
    assert metrics.total_events == 1
    assert metrics.failed == 1


def test_projection_contract_owns_dashboard_snapshot_topics() -> None:
    contract_path = Path("src/omnimarket/nodes/node_omnigate_projection/contract.yaml")
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))

    topics = {snapshot["topic"] for snapshot in contract["projection_api"]["snapshots"]}
    assert topics == {
        "onex.snapshot.projection.gate.activity.v1",
        "onex.snapshot.projection.gate.metrics.v1",
    }
    assert contract["node_type"] == "reducer"
