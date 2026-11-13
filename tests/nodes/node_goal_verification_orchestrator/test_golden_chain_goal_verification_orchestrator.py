# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20033: golden and error chain through the registered contract on the in-memory bus."""

from __future__ import annotations

import json
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult

import omnimarket.nodes.node_goal_verification_orchestrator as node_package
from omnimarket.nodes.node_goal_verification_orchestrator.models.model_goal_verification import (
    EnumGoalVerificationAction,
    ModelGoalVerificationOutcome,
)
from tests.nodes.node_goal_verification_orchestrator.support import result_event
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent


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


def test_the_contract_declares_its_topics_and_the_verifier_start_publish() -> None:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    bus = contract["event_bus"]
    assert "onex.evt.omniclaude.work-result-recorded.v1" in bus["subscribe_topics"]
    assert "onex.evt.omnibase-infra.delegation-completed.v2" in bus["subscribe_topics"]
    assert "onex.evt.omnimarket.goal-disposition-recorded.v1" in bus["subscribe_topics"]
    # OMN-24236: the returned outcome has its own decided topic; the verifier start command
    # stays declared because the injected publisher sends it there.
    assert bus["publish_topics"] == [
        "onex.evt.omnimarket.goal-verification-decided.v1",
        "onex.cmd.omnimarket.dod-verify-start.v1",
    ]
    assert (
        contract["verification_rules"]["start_command_topic"] in bus["publish_topics"]
    )
    routed = {entry["topic"] for entry in contract["handler_routing"]["handlers"]}
    assert routed == set(bus["subscribe_topics"])


def test_entry_point_is_registered() -> None:
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_goal_verification_orchestrator"].load() is node_package


@pytest.mark.asyncio
async def test_golden_chain_a_result_that_closes_a_goal_is_handled(
    tmp_path: Path,
) -> None:
    payload = json.loads(result_event(1).model_dump_json())
    runtime = _runtime(tmp_path, payload, "golden")
    await runtime.run_async()
    result = runtime.handler_result
    assert isinstance(result, ModelGoalVerificationOutcome)
    [decision] = result.decisions
    # The default wiring has no goal contract reader, so the close is listed, not lost.
    assert decision.action is EnumGoalVerificationAction.CONTRACT_UNREAD
    assert str(decision.goal_id) in payload["closes_claims"]


@pytest.mark.asyncio
async def test_error_chain_a_result_with_no_friction_record_is_refused(
    tmp_path: Path,
) -> None:
    payload = json.loads(result_event(1).model_dump_json())
    payload["friction_none"] = False
    refused = _runtime(tmp_path, payload, "bad")
    assert await refused.run_async() is EnumWorkflowResult.FAILED
    assert refused.handler_result is None
