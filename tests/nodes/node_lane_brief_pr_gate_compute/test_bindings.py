# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC9: the gate depends on no protocol, and runs the same in process and through the local entry."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import JsonValue

from omnimarket.models.lane_brief_pr_gate.model_lane_brief_pr_gate_request import (
    ModelLaneBriefPrGateRequest,
)
from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_parity_run import (
    HandlerParityRun,
)
from omnimarket.nodes.node_binding_parity_orchestrator.handlers.handler_run_local import (
    run_local,
)
from omnimarket.nodes.node_binding_parity_orchestrator.models.model_parity import (
    ModelParityRequest,
    ModelParityScenario,
)
from omnimarket.nodes.node_binding_parity_orchestrator.models.model_run_local import (
    ModelRunLocalConfig,
)
from omnimarket.nodes.node_lane_brief_pr_gate_compute.handlers.handler_lane_brief_pr_gate import (
    HandlerLaneBriefPrGate,
)
from tests.nodes.node_binding_parity_orchestrator.test_parity import (
    FIXED_NOW,
    _fixture_overlay,
)
from tests.nodes.node_binding_parity_orchestrator.test_parity import (
    NODE_DIR as PARITY_NODE_DIR,
)
from tests.nodes.node_lane_brief_pr_gate_compute.test_pr_gate import (
    DISPATCH,
    ENFORCE,
    FULL,
    NOW,
    ROOM,
    _policy,
    brief,
)

pytestmark = pytest.mark.unit

NODE = "node_lane_brief_pr_gate_compute"
NODE_DIR = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes" / NODE


def _payload(text: str, state: dict[str, Any]) -> dict[str, JsonValue]:
    request = ModelLaneBriefPrGateRequest(
        brief=text,
        dispatch_id=DISPATCH,
        now=NOW,
        state=state,
        config=ENFORCE,
        policy=_policy(),
    )
    return json.loads(request.model_dump_json())


FIXTURES: dict[str, tuple[str, dict[str, Any]]] = {
    "missing_line": (brief(None), ROOM),
    "at_limit": (brief(), FULL),
    "under_limit": (brief(), ROOM),
    "no_pr": (brief("none"), FULL),
}


def _local_config() -> ModelRunLocalConfig:
    contract = yaml.safe_load(
        (PARITY_NODE_DIR / "contract.yaml").read_text(encoding="utf-8")
    )
    return ModelRunLocalConfig.model_validate(contract["config"]["run_local"])


def test_the_contract_declares_no_protocol_dependency() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert "protocol_dependencies" not in contract
    assert contract["descriptor"]["purity"] == "pure"
    assert not (NODE_DIR / "protocols").exists()


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_in_process_and_the_local_entry_give_byte_identical_results(name: str) -> None:
    text, state = FIXTURES[name]
    payload = _payload(text, state)
    request = ModelLaneBriefPrGateRequest.model_validate(payload)
    in_process = HandlerLaneBriefPrGate().handle(request).model_dump_json()
    local = asyncio.run(
        run_local(NODE, "check_lane_brief_pr", payload, _local_config())
    )
    assert json.dumps(local, sort_keys=True) == json.dumps(
        json.loads(in_process), sort_keys=True
    )


@pytest.mark.parametrize("name", sorted(FIXTURES))
def test_the_parity_run_passes_under_two_profiles(name: str) -> None:
    text, state = FIXTURES[name]
    result = HandlerParityRun().handle(
        ModelParityRequest(
            scenario=ModelParityScenario(
                kind="node_operation",
                node=NODE,
                operation="check_lane_brief_pr",
                fixture={"request": _payload(text, state)},
            ),
            overlay=_fixture_overlay(),
            fixed_now=FIXED_NOW,
        )
    )
    assert result.passed, result.first_difference


def test_the_contract_declares_the_command_and_checked_topics() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.cmd.omnimarket.lane-brief-pr-gate-requested.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.lane-brief-pr-gate-checked.v1"
    ]
