# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC4 of OMN-20017, golden chain: a request is answered once with ModelComparisonResult and its verdict."""

from __future__ import annotations

import importlib.resources
from importlib import import_module
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml

from omnimarket.adapters.codex.local_runtime_dispatch import (
    LocalRuntimeDispatch,
    _resolve_node_route,
)
from omnimarket.adapters.codex.runtime_client import (
    ModelDispatchBusCommand,
    default_command_topic,
    default_response_topic,
)
from omnimarket.models.delegation_arm_compare.model_arm_compare_request import (
    ModelArmCompareRequest,
)
from omnimarket.models.delegation_arm_compare.model_arm_compare_result import (
    ModelArmCompareResult,
)
from omnimarket.models.ranges.enum_comparison_verdict import EnumComparisonVerdict
from omnimarket.nodes.node_delegation_arm_compare_compute.handlers.handler_delegation_arm_compare import (
    HandlerDelegationArmCompare,
)
from tests.nodes.node_delegation_arm_compare_compute.support import planted, request

pytestmark = pytest.mark.unit

NAME = "node_delegation_arm_compare_compute"
COMMAND_TOPIC = "onex.cmd.omnimarket.delegation-arm-compare-requested.v1"
ANSWER_TOPIC = "onex.evt.omnimarket.delegation-arm-compare-measured.v1"
OVERLAY_ROOT = Path(__file__).resolve().parent / "fixtures" / "overlays"


def contract() -> dict[str, Any]:
    path = importlib.resources.files(f"omnimarket.nodes.{NAME}").joinpath(
        "contract.yaml"
    )
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def planted_request() -> ModelArmCompareRequest:
    a, b = planted(both_pass=70, both_fail=6, a_only=2, b_only=12)
    return request(a, b)


def test_contract_declares_one_operation_and_the_two_topics() -> None:
    c = contract()
    assert c["node_type"] == "compute"
    assert [h["operation"] for h in c["handler_routing"]["handlers"]] == [
        "delegation_arm_compare"
    ]
    assert c["event_bus"]["subscribe_topics"] == [COMMAND_TOPIC]
    assert c["event_bus"]["publish_topics"] == [ANSWER_TOPIC]
    assert c["terminal_event"] == ANSWER_TOPIC
    assert c["runtime_dispatch"]["command_topic"] == COMMAND_TOPIC


def test_registered_route_names_the_topics_and_the_typed_models() -> None:
    route = _resolve_node_route(NAME)
    assert route.command_topic == COMMAND_TOPIC
    assert route.terminal_topic == ANSWER_TOPIC
    assert (
        getattr(import_module(route.handler_module), route.handler_class)
        is HandlerDelegationArmCompare
    )
    assert (
        getattr(import_module(route.input_model_module), route.input_model_name)
        is ModelArmCompareRequest
    )


@pytest.mark.asyncio
async def test_golden_chain_request_is_answered_once_with_the_exact_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(OVERLAY_ROOT))
    runtime = LocalRuntimeDispatch(
        adapter_command_topic=default_command_topic(), state_root=tmp_path
    )
    terminal, evidence = await runtime.dispatch(
        ModelDispatchBusCommand(
            command_name=NAME,
            requester="arm-compare-golden-chain",
            payload=planted_request().model_dump(mode="json"),
            correlation_id=uuid4(),
            response_topic=default_response_topic(),
            timeout_seconds=10,
        )
    )
    assert terminal.status == "completed", terminal.error_message
    assert evidence.runtime_observation.status == "OBSERVED"
    assert terminal.payload is not None
    answer = ModelArmCompareResult.model_validate(terminal.payload)
    assert answer.failure is None
    assert answer.result is not None
    assert answer.result.verdict is EnumComparisonVerdict.DIFFERENCE
    assert (answer.result.a_only, answer.result.b_only) == (2, 12)
    assert answer.control is None
