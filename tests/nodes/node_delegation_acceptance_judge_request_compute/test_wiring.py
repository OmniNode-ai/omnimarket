# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A completed delegation reaches the acceptance judge: its command topic has a producer."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.handlers.handler_delegation_acceptance_judge import (
    HandlerDelegationAcceptanceJudge,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.enum_acceptance_operation import (
    EnumAcceptanceOperation,
)
from omnimarket.nodes.node_delegation_acceptance_judge_compute.models.model_acceptance_judge_request import (
    ModelAcceptanceJudgeRequest,
)
from omnimarket.nodes.node_delegation_acceptance_judge_request_compute.handlers.handler_delegation_acceptance_judge_request import (
    HandlerDelegationAcceptanceJudgeRequest,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
JUDGE_COMMAND = "onex.cmd.omnimarket.delegation-acceptance-judge-requested.v1"
JUDGED_EVENT = "onex.evt.omnimarket.delegation-acceptance-judged.v1"
COMPLETED = "onex.evt.omnimarket.delegate-skill-completed.v1"
NODE = "node_delegation_acceptance_judge_request_compute"
TASK = (
    "Summarise the three facts below in two sentences.\nfact one\nfact two\nfact three"
)
ANSWER = "Fact one and two hold. Fact three follows."


def _contracts() -> dict[str, dict[str, Any]]:
    return {
        path.parent.name: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted(NODES.glob("node_*/contract.yaml"))
    }


def _completed() -> ModelDelegateSkillCompleted:
    return ModelDelegateSkillCompleted(
        correlation_id=uuid4(),
        task_type="document",
        model_name="the-served-model",
        prompt_text=TASK,
        response=ANSWER,
        quality_gate_passed=True,
        quality_score=1.0,
    )


def test_judge_command_topic_has_a_producer_contract() -> None:
    publishers = [
        name
        for name, contract in _contracts().items()
        if JUDGE_COMMAND
        in ((contract.get("event_bus") or {}).get("publish_topics") or [])
    ]
    assert publishers == [NODE]


def test_producer_subscribes_to_the_completed_delegation_terminal() -> None:
    bus = _contracts()[NODE]["event_bus"]
    assert bus["subscribe_topics"] == [COMPLETED]
    assert bus["publish_topics"] == [JUDGE_COMMAND]


def test_registered_route_resolves_to_the_typed_handler() -> None:
    route = _resolve_node_route(NODE)
    assert route.command_topic == COMPLETED
    assert route.terminal_topic == JUDGE_COMMAND
    assert (
        getattr(import_module(route.handler_module), route.handler_class)
        is HandlerDelegationAcceptanceJudgeRequest
    )
    assert (
        getattr(import_module(route.input_model_module), route.input_model_name)
        is ModelDelegateSkillCompleted
    )


def test_completed_delegation_becomes_a_render_request_the_judge_accepts() -> None:
    completed = _completed()
    request = HandlerDelegationAcceptanceJudgeRequest().handle(completed)
    assert isinstance(request, ModelAcceptanceJudgeRequest)
    assert request.operation is EnumAcceptanceOperation.RENDER
    assert request.seed == str(completed.correlation_id)
    (item,) = request.items
    assert item.item_id == str(completed.correlation_id)
    assert (item.task_type, item.task_text, item.answer_text) == (
        "document",
        TASK,
        ANSWER,
    )

    result = HandlerDelegationAcceptanceJudge().handle(request)
    (batch,) = (b for b in result.batches if b.role == "primary")
    assert TASK in batch.prompt
    assert ANSWER in batch.prompt
    assert "the-served-model" not in batch.prompt


def test_request_round_trips_through_json() -> None:
    request = HandlerDelegationAcceptanceJudgeRequest().handle(_completed())
    assert set(request.model_dump()) >= set(_contracts()[NODE]["outputs"])
    assert request.model_dump()["rubric_yaml"]
    assert (
        ModelAcceptanceJudgeRequest.model_validate_json(request.model_dump_json())
        == request
    )


def test_node_is_an_entry_point_in_pyproject() -> None:
    import tomllib

    root = Path(__file__).resolve().parents[3]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    points = pyproject["project"]["entry-points"]["onex.nodes"]
    assert points[NODE] == f"omnimarket.nodes.{NODE}"
