# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the contract's declared handler takes the plan and returns a run report with a row per host."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_worktree_prune_workflow_effect.models.model_worktree_prune_workflow import (
    ModelHostPruneResult,
    ModelPrunedTree,
    ModelPruneHostTarget,
    ModelWorktreePrunePlan,
    ModelWorktreePruneRunReport,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_worktree_prune_workflow_effect/contract.yaml"
)


def _activity(
    target: ModelPruneHostTarget, step_key: str, attempt: int
) -> ModelHostPruneResult:
    used_after = 90.0 if target.host == "h105" else 60.0
    return ModelHostPruneResult(
        host=target.host,
        disk_used_percent_before=used_after + 1,
        disk_used_percent_after=used_after,
        removed=(ModelPrunedTree(path=f"/{target.host}/merged", freed_bytes=2048),),
    )


def test_golden_chain_contract_handler_plan_to_report() -> None:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    handler_cls = getattr(
        importlib.import_module(contract["handler"]["module"]),
        contract["handler"]["class"],
    )
    module_name, _, model_name = contract["input_model"].rpartition(".")
    plan_model = getattr(importlib.import_module(module_name), model_name)
    assert plan_model is ModelWorktreePrunePlan

    plan = plan_model.model_validate(
        {
            "run_id": "golden-1",
            "trigger": "ticket_closed",
            "ticket": "OMN-1",
            "hosts": [
                {"host": "h201", "disk_bar_percent": 85},
                {"host": "h105", "disk_bar_percent": 85},
            ],
        }
    )
    report = handler_cls(_activity).handle(plan)

    assert isinstance(report, ModelWorktreePruneRunReport)
    assert [(row.host, row.status) for row in report.rows] == [
        ("h201", "ok"),
        ("h105", "ok"),
    ]
    assert report.freed_bytes == 4096
    assert [alarm.host for alarm in report.alarms] == ["h105"]
    assert all(
        row.result is not None
        and row.result.disk_used_percent_before > row.result.disk_used_percent_after
        for row in report.rows
    )
