# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23851: the goal-scope admission over its contract on the in-memory bus, golden and error chain."""

from __future__ import annotations

import importlib
import json
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.enums.enum_workflow_result import EnumWorkflowResult
from pydantic import ValidationError

import omnimarket.nodes.node_goal_scope_admission_compute as node_package
from omnimarket.models.goal_scope_admission import (
    ModelGoalScopeAdmissionRequest,
    ModelGoalScopeAdmissionResult,
)
from omnimarket.nodes.node_goal_scope_admission_compute.tests.support import (
    CASES,
    request_of,
)
from tests.runtime_local_compat import RuntimeLocal

pytestmark = pytest.mark.unit

NODE_DIR = Path(node_package.__file__).parent
COMMAND_TOPIC = "onex.cmd.omnimarket.goal-scope-admission-requested.v1"
TERMINAL_TOPIC = "onex.evt.omnimarket.goal-scope-admission-completed.v1"


def test_contract_declares_topics_models_handler_and_entry_point() -> None:
    contract: dict[str, Any] = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["purity"] == "pure"
    assert contract["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC
    assert contract["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert contract["event_bus"]["publish_topics"] == [TERMINAL_TOPIC]
    assert contract["terminal_event"] == TERMINAL_TOPIC
    binding = contract["handler"]
    handler_type = getattr(importlib.import_module(binding["module"]), binding["class"])
    assert issubclass(node_package.NodeGoalScopeAdmissionCompute, handler_type)
    for side, model in (
        ("input_model", ModelGoalScopeAdmissionRequest),
        ("output_model", ModelGoalScopeAdmissionResult),
    ):
        declared = getattr(
            importlib.import_module(contract[side]["module"]), contract[side]["name"]
        )
        assert declared is model
    registered = {e.name: e for e in entry_points(group="onex.nodes")}
    assert registered["node_goal_scope_admission_compute"].load() is node_package


async def _run(tmp_path: Path, name: str, payload: dict[str, Any]) -> RuntimeLocal:
    (tmp_path / name).mkdir()
    input_path = tmp_path / name / "request.json"
    input_path.write_text(json.dumps(payload))
    runtime = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=input_path,
        state_root=tmp_path / name / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    await runtime.run_async()
    return runtime


@pytest.mark.asyncio
async def test_golden_chain_over_the_bus_matches_a_recorded_case(
    tmp_path: Path,
) -> None:
    case = next(
        c for c in CASES if c["name"] == "rebuild_pieces_and_one_runtime_two_roots"
    )
    payload = json.loads(request_of(case).model_dump_json())
    runtime = await _run(tmp_path, "golden", payload)
    result = runtime.handler_result
    assert isinstance(result, ModelGoalScopeAdmissionResult)
    assert {r: [v.via, v.label] for r, v in sorted(result.admitted.items())} == case[
        "expected"
    ]["admitted"]
    assert "omnimarket#5150" not in result.admitted
    assert (
        ModelGoalScopeAdmissionResult.model_validate_json(result.model_dump_json())
        == result
    )


@pytest.mark.asyncio
async def test_golden_chain_over_the_bus_names_an_unread_ticket_and_admits_nothing(
    tmp_path: Path,
) -> None:
    case = next(
        c for c in CASES if c["name"] == "unreadable_linear_holds_everything_unread"
    )
    runtime = await _run(
        tmp_path, "unread", json.loads(request_of(case).model_dump_json())
    )
    result = runtime.handler_result
    assert isinstance(result, ModelGoalScopeAdmissionResult)
    assert result.admitted == {}
    assert sorted(result.unread) == sorted(case["candidates"])


@pytest.mark.asyncio
async def test_error_chain_over_the_bus_fails_without_a_result(tmp_path: Path) -> None:
    runtime = await _run(tmp_path, "empty", {})
    assert runtime.handler_result is None
    (tmp_path / "bad").mkdir()
    bad = tmp_path / "bad" / "request.json"
    bad.write_text(json.dumps({"candidates": {"r#1": {"title": "x", "surprise": 1}}}))
    refused = RuntimeLocal(
        workflow_path=NODE_DIR / "contract.yaml",
        input_path=bad,
        state_root=tmp_path / "bad" / "state",
        backend_overrides={"event_bus": "inmemory"},
        timeout=10,
    )
    assert await refused.run_async() is EnumWorkflowResult.FAILED
    assert refused.handler_result is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"candidates": {"r#1": {"title": "x", "surprise": 1}}},
        {"candidates": {}, "rule": {"roots": ["OMN-1"]}},
        {"candidates": {}, "ticket_facts": {"OMN-1": {"parent": 5}}},
    ],
)
def test_error_chain_refuses_a_malformed_request_before_the_handler_runs(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        ModelGoalScopeAdmissionRequest.model_validate(payload)
