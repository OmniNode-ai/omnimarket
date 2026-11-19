# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain of node_lane_container_budget_compute: the command its contract declares, the handler, the verdict event.

The two topics are read from the node's own contract, so a contract that stops declaring them fails here.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.model_lane_container_budget import (
    ModelLaneContainerBudgetDeclaration,
    ModelLaneContainerBudgetRequest,
    ModelLaneContainerBudgetVerdict,
)
from omnimarket.nodes.node_lane_container_budget_compute.handlers.handler_lane_container_budget import (
    HandlerLaneContainerBudget,
)
from tests.chains.chain_assert import ChainRecorder, assert_chain

pytestmark = pytest.mark.unit

NODE_DIR = Path(__file__).resolve().parents[3] / (
    "src/omnimarket/nodes/node_lane_container_budget_compute"
)
FIXTURE = Path(__file__).parent / "fixtures" / "docker_ps_over_budget.ndjson"


def _contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(
        (NODE_DIR / "contract.yaml").read_text(encoding="utf-8")
    )
    return loaded


def _request(budget: int) -> ModelLaneContainerBudgetRequest:
    return ModelLaneContainerBudgetRequest(
        host="host-a",
        docker_ps=FIXTURE.read_text(encoding="utf-8"),
        declarations=(
            ModelLaneContainerBudgetDeclaration(
                host="host-a",
                lane="alpha",
                compose_project="proj-alpha",
                application_container_budget=budget,
                infrastructure_markers=("redpanda", "postgres"),
            ),
        ),
    )


async def _run(
    budget: int,
) -> tuple[ChainRecorder, ModelLaneContainerBudgetVerdict, str]:
    contract = _contract()
    command = contract["runtime_dispatch"]["command_topic"]
    terminal = contract["terminal_event"]
    key = uuid.uuid4().hex
    recorder = ChainRecorder(EventBusInmemory())
    request = _request(budget)
    await recorder.publish(command, request, correlation_id=key)
    verdict = HandlerLaneContainerBudget().handle(request)
    await recorder.publish(terminal, verdict, correlation_id=key)
    return recorder, verdict, key


async def test_command_on_the_declared_topic_is_judged_and_answered_pass() -> None:
    recorder, verdict, key = await _run(3)
    assert verdict.status == "PASS"
    assert_chain(
        recorder.events,
        expected_event_types=[
            ModelLaneContainerBudgetRequest.__name__,
            ModelLaneContainerBudgetVerdict.__name__,
        ],
        terminal_fields={"status": "PASS", "failed": False},
        correlation_id=key,
        bus_history_count=await recorder.bus_history_count(),
    )


async def test_command_over_budget_is_answered_fail_with_one_alarm() -> None:
    recorder, verdict, key = await _run(2)
    assert verdict.status == "FAIL"
    assert [alarm.key for alarm in verdict.alarms] == ["lane-budget:host-a:alpha"]
    assert_chain(
        recorder.events,
        expected_event_types=[
            ModelLaneContainerBudgetRequest.__name__,
            ModelLaneContainerBudgetVerdict.__name__,
        ],
        terminal_fields={"status": "FAIL", "failed": True},
        correlation_id=key,
        bus_history_count=await recorder.bus_history_count(),
    )
