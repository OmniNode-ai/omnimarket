"""Focused checks for OMN-12322 high-risk orchestration handlers."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_IMPLEMENTED_NODES = (
    "node_epic_team_orchestrator",
    "node_multi_agent_orchestrator",
    "node_self_healing_dispatch_orchestrator",
    "node_dispatch_watchdog_orchestrator",
    "node_wave_scheduler_orchestrator",
    "node_refill_sprint_orchestrator",
    "node_runner_orchestrator",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.mark.unit
@pytest.mark.parametrize("node_name", _IMPLEMENTED_NODES)
def test_high_risk_orchestration_contracts_are_not_stubbed(node_name: str) -> None:
    contract_path = (
        _repo_root() / "src" / "omnimarket" / "nodes" / node_name / "contract.yaml"
    )

    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))

    assert raw["node_not_implemented"] is False
