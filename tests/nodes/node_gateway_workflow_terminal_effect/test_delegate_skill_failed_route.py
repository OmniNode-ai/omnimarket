# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The terminal writer hears the malformed command terminal of the delegate-skill command (OMN-17214).

C28's dying run submits an unknown ``task_type`` to ``onex.cmd.omnimarket.delegate-skill.v1``. The
dispatch engine refuses it as ``publisher_malformed`` and publishes the typed terminal on the failure
topic that contract declares, ``delegate-skill-failed``. The writer subscribed only to the
omnibase-infra delegation pair, so the row stayed ``published`` and the run never read ``failed``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from omnimarket.nodes.node_gateway_workflow_terminal_effect.handlers.handler_gateway_workflow_terminal import (
    HandlerGatewayWorkflowTerminal,
    load_contract_routes,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.models.model_delegation_terminal_envelope import (
    ModelDelegationTerminalEnvelope,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.models.model_gateway_terminal_applied import (
    EnumGatewayTerminalOutcome as Outcome,
)
from tests.nodes.node_gateway_workflow_terminal_effect.fakes import (
    FakeDeadLetter,
    FakeRow,
    FakeTerminalStore,
)

_NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
_TERMINAL_CONTRACT = yaml.safe_load(
    (_NODES / "node_gateway_workflow_terminal_effect/contract.yaml").read_text("utf-8")
)
_SKILL_CONTRACT = yaml.safe_load(
    (_NODES / "node_delegate_skill_orchestrator/contract.yaml").read_text("utf-8")
)
_SKILL_FAILED = _SKILL_CONTRACT["runtime_dispatch"]["terminal_events"]["failure"]
_SKILL_COMMAND = _SKILL_CONTRACT["runtime_dispatch"]["command_topic"]
_TENANT = uuid4()
_CORR = uuid4()


def _malformed(**overrides: Any) -> dict[str, Any]:
    wire: dict[str, Any] = {
        "topic": _SKILL_FAILED,
        "correlation_id": str(_CORR),
        "workflow_id": None,
        "status": "failed",
        "failure_class": "publisher_malformed",
        "failure_code": "ONEX_CORE_345_ENVELOPE_VALIDATION_FAILED",
        "retryable": False,
        "failure_reason": "task_type: Input should be a valid task class",
        "origin_topic": _SKILL_COMMAND,
    }
    wire.update(overrides)
    return wire


def _make() -> tuple[HandlerGatewayWorkflowTerminal, FakeTerminalStore, FakeDeadLetter]:
    store = FakeTerminalStore([FakeRow(_TENANT, _CORR)])
    dlq = FakeDeadLetter()
    return HandlerGatewayWorkflowTerminal(store, dlq), store, dlq


def test_the_contract_subscribes_to_and_routes_the_delegate_skill_failure_topic() -> (
    None
):
    routes, _ = load_contract_routes()
    assert routes.get(_SKILL_FAILED) == "failed"
    assert _SKILL_FAILED in _TERMINAL_CONTRACT["event_bus"]["subscribe_topics"]
    grants = {
        (g["name"], g["operation"])
        for acl in _TERMINAL_CONTRACT["broker_acl_grants"]
        for g in acl["grants"]
    }
    assert (_SKILL_FAILED, "READ") in grants


async def test_a_malformed_delegate_skill_command_closes_its_row_failed() -> None:
    """Known good: the terminal the engine publishes for the refused command ends the run."""
    handler, store, dlq = _make()
    result = await handler.handle(
        ModelDelegationTerminalEnvelope(topic=_SKILL_FAILED, payload=_malformed())
    )
    assert result.outcome is Outcome.APPLIED
    assert store.rows[0].status == "failed"
    assert store.rows[0].columns["failure_class"] == "PublisherMalformedError"
    assert dlq.records == []


async def test_an_ordinary_delegate_skill_failure_is_left_to_its_own_writer() -> None:
    """Known bad: any other failure on that topic is neither written nor dead-lettered here."""
    handler, store, dlq = _make()
    result = await handler.handle(
        ModelDelegationTerminalEnvelope(
            topic=_SKILL_FAILED,
            payload=_malformed(failure_class="handler_error", model_used="m"),
        )
    )
    assert result.outcome is Outcome.NOT_A_GATEWAY_WORKFLOW
    assert store.rows[0].status == "published"
    assert dlq.records == []
