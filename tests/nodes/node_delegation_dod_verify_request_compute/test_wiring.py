# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A ticketed delegation reaches dod_verify naming its run (OMN-19514).

Before this node, ``dod_verify`` accepted ``delegation_correlation_id`` and the
verdict projection stored it, but nothing published a start command carrying one,
so no ``dod_verify_runs`` row joined to a ``delegation_events`` row.
"""

from __future__ import annotations

import tomllib
from importlib import import_module
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route
from omnimarket.enums.enum_dod_verify_execution_audience import (
    EnumDodVerifyExecutionAudience,
)
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_delegation_dod_verify_request_compute.handlers.handler_delegation_dod_verify_request import (
    HandlerDelegationDodVerifyRequest,
)
from omnimarket.nodes.node_delegation_dod_verify_request_compute.models.model_delegation_dod_verify_request import (
    ModelDelegationDodVerifyRequest,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
NODES = ROOT / "src" / "omnimarket" / "nodes"
NODE = "node_delegation_dod_verify_request_compute"
COMPLETED = "onex.evt.omnimarket.delegate-skill-completed.v1"
VERIFY_START = "onex.cmd.omnimarket.dod-verify-start.v1"


def _contracts() -> dict[str, dict[str, Any]]:
    return {
        path.parent.name: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in sorted(NODES.glob("node_*/contract.yaml"))
    }


def _terminal(**overrides: Any) -> ModelDelegateSkillTerminalProjection:
    fields: dict[str, Any] = {
        "status": "completed",
        "correlation_id": uuid4(),
        "task_type": "document",
        "model_name": "the-served-model",
        "prompt_text": "Summarise the facts.",
        "response": "Fact one holds.",
        "quality_gate_passed": True,
        "quality_score": 1.0,
        "ticket_id": "OMN-19514",
    }
    fields.update(overrides)
    return ModelDelegateSkillTerminalProjection(**fields)


def test_start_topic_has_this_node_as_a_producer_and_dod_verify_as_its_consumer() -> (
    None
):
    contracts = _contracts()
    publishers = [
        name
        for name, contract in contracts.items()
        if VERIFY_START
        in ((contract.get("event_bus") or {}).get("publish_topics") or [])
    ]
    assert NODE in publishers
    assert contracts["node_dod_verify"]["event_bus"]["subscribe_topics"] == [
        VERIFY_START
    ]
    bus = contracts[NODE]["event_bus"]
    assert bus["subscribe_topics"] == [COMPLETED]
    assert bus["publish_topics"] == [VERIFY_START]


def test_registered_route_resolves_to_the_typed_handler() -> None:
    route = _resolve_node_route(NODE)
    assert route.command_topic == COMPLETED
    assert route.terminal_topic == VERIFY_START
    assert (
        getattr(import_module(route.handler_module), route.handler_class)
        is HandlerDelegationDodVerifyRequest
    )
    assert (
        getattr(import_module(route.input_model_module), route.input_model_name)
        is ModelDelegateSkillTerminalProjection
    )


def test_ticketed_delegation_becomes_a_start_command_naming_its_run() -> None:
    completed = _terminal()
    request = HandlerDelegationDodVerifyRequest().handle(completed)
    assert isinstance(request, ModelDelegationDodVerifyRequest)
    assert request.ticket_id == "OMN-19514"
    assert request.delegation_correlation_id == completed.correlation_id
    assert request.correlation_id != completed.correlation_id
    assert request.execution_audience is EnumDodVerifyExecutionAudience.HOSTED


def test_the_request_is_a_start_command_dod_verify_accepts_and_carries_to_the_verdict() -> (
    None
):
    completed = _terminal()
    request = HandlerDelegationDodVerifyRequest().handle(completed)
    assert request is not None
    command = ModelDodVerifyStartCommand.model_validate_json(request.model_dump_json())
    assert command.ticket_id == "OMN-19514"
    assert command.delegation_correlation_id == completed.correlation_id
    assert command.correlation_id == request.correlation_id
    assert command.execution_audience is EnumDodVerifyExecutionAudience.HOSTED


def test_a_redelivered_terminal_asks_for_the_same_verification_run() -> None:
    completed = _terminal()
    handler = HandlerDelegationDodVerifyRequest()
    first, second = handler.handle(completed), handler.handle(completed)
    assert first is not None
    assert first == second
    other = handler.handle(_terminal())
    assert other is not None
    assert other.correlation_id != first.correlation_id


def test_output_fields_are_declared_by_the_contract() -> None:
    request = HandlerDelegationDodVerifyRequest().handle(_terminal())
    assert request is not None
    assert set(request.model_dump()) == set(_contracts()[NODE]["outputs"])


@pytest.mark.parametrize(
    "overrides",
    [
        {"ticket_id": None},
        {"ticket_id": "not-a-ticket"},
        {"ticket_id": "omn-19514"},
        {"ticket_id": "OMN-0"},
        {"ticket_id": 19514},
    ],
    ids=["no-ticket", "malformed", "lowercase", "zero", "non-string"],
)
def test_a_terminal_without_a_valid_ticket_publishes_nothing(
    overrides: dict[str, Any],
) -> None:
    assert HandlerDelegationDodVerifyRequest().handle(_terminal(**overrides)) is None


@pytest.mark.parametrize("status", ["failed", "timeout"])
def test_a_delegation_that_did_not_complete_publishes_nothing(status: str) -> None:
    terminal = _terminal(
        status=status,
        response="",
        quality_gate_passed=False,
        quality_score=0.0,
        error_message="provider refused",
    )
    assert terminal.status == status
    assert HandlerDelegationDodVerifyRequest().handle(terminal) is None


def test_the_verification_run_id_is_pinned_to_the_delegation_run() -> None:
    delegation = UUID("00000000-0000-4000-8000-000000000001")
    request = HandlerDelegationDodVerifyRequest().handle(
        _terminal(correlation_id=delegation)
    )
    assert request is not None
    assert request.correlation_id == UUID("9a4f2145-0197-52eb-856e-9a9063558a3c")


def test_node_is_an_entry_point_in_pyproject() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    points = pyproject["project"]["entry-points"]["onex.nodes"]
    assert points[NODE] == f"omnimarket.nodes.{NODE}"
