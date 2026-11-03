# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: overlay budgets in, the node entry point out, one finding per stalled lane."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_work_progress_compute import NodeWorkProgressCompute
from omnimarket.nodes.node_work_progress_compute.models import (
    ModelWorkProgressRequest,
    ModelWorkProgressSubject,
)
from omnimarket.nodes.node_work_progress_compute.overlay import (
    NODE_NAME,
    load_budgets,
    load_phase_notes,
)
from tests.node_overlay_support import install_node_overlay

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def test_golden_chain_node_work_progress_compute_reports_a_stalled_claimed_lane(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(
        monkeypatch,
        tmp_path,
        NODE_NAME,
        {"budget_seconds": {"placed": 900, "claimed": 3600, "pr_open": 5400}},
    )
    request = ModelWorkProgressRequest(
        now=NOW,
        budgets=load_budgets(),
        phase_notes=load_phase_notes(),
        subjects=(
            ModelWorkProgressSubject(
                lane="rf0925-c2",
                phase="claimed",
                entered_at=NOW - timedelta(minutes=64),
                entering_row_id="row-claimed-1",
                owner="orchestrator-31d9",
            ),
            ModelWorkProgressSubject(
                lane="rf0925-c3",
                phase="claimed",
                entered_at=NOW - timedelta(minutes=59),
                entering_row_id="row-claimed-2",
                owner="orchestrator-31d9",
            ),
        ),
    )
    result = NodeWorkProgressCompute().handle(request)
    assert [f.lane for f in result.findings] == ["rf0925-c2"]
    assert result.findings[0].text == (
        "Lane rf0925-c2 has been in claimed for 64 minutes against a 60 minute "
        "budget; no PR is recorded in the ledger. Owner: orchestrator-31d9."
    )
    assert result.findings[0].idempotency_key == (
        "work-progress|lane:rf0925-c2|claimed|row-claimed-1"
    )
