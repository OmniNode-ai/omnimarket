# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21974: the stop decision through the registered contract on the in-memory bus."""

from __future__ import annotations

import json
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

import omnimarket.nodes.node_goal_run_stop_compute as node_package
from omnimarket.models.model_goal_run import EnumGoalRunAction, EnumGoalStopReason
from omnimarket.nodes.node_goal_run_stop_compute.models.model_goal_run_stop import (
    ModelGoalRunStopDecision,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent

PAYLOAD: dict[str, object] = {
    "correlation_id": "cm13-golden",
    "item_id": "item-1",
    "goal": {
        "predicate_kind": "verify_run",
        "predicate_ref": "uv run pytest tests/x -q",
        "predicate_sha256": "a" * 64,
    },
    "caps_file_text": (
        "token_cap: 300000\nwall_clock_cap_s: 7200\nturn_cap: 40\n"
        "compaction_cap: 3\nno_progress_window_turns: 8\n"
    ),
    "counters": {
        "tokens": 300000,
        "wall_clock_s": 600,
        "turns": 12,
        "compactions": 1,
        "turns_without_subcheck_change": 0,
    },
}


def _runtime(tmp_path: Path, payload: dict[str, object], name: str) -> RuntimeLocal:
    input_path = tmp_path / f"{name}.json"
    input_path.write_text(json.dumps(payload))
    (tmp_path / name).mkdir()
    return RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / name,
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )


def test_the_contract_is_pure_and_declares_no_topic_and_no_subscription() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["descriptor"]["purity"] == "pure"
    assert not contract.get("event_bus", {}).get("subscribe_topics")
    assert not contract.get("event_bus", {}).get("publish_topics")
    assert contract["metadata"]["transport_type"] == "local"
    assert contract["descriptor"]["runtime_profiles"] == []


def test_entry_point_is_registered() -> None:
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_goal_run_stop_compute"].load() is node_package


@pytest.mark.asyncio
async def test_golden_chain_a_token_capped_run_is_censored(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, PAYLOAD, "capped")
    await runtime.run_async()
    result = runtime.handler_result
    assert isinstance(result, ModelGoalRunStopDecision)
    assert result.action is EnumGoalRunAction.STOP
    assert result.stop_reason is EnumGoalStopReason.TOKEN_CAP
    assert result.analysis is not None
    assert result.analysis.right_censored is True
    assert result.correlation_id == "cm13-golden"


@pytest.mark.asyncio
async def test_error_chain_a_malformed_caps_file_fails_without_a_result(
    tmp_path: Path,
) -> None:
    refused = _runtime(tmp_path, {**PAYLOAD, "caps_file_text": "token_cap: 0\n"}, "bad")
    assert await refused.run_async() is EnumWorkflowResult.FAILED
    assert refused.handler_result is None
