# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A delegate-skill command the engine refused closes its gateway row failed (OMN-17358).

Scheduled checks C14, C16 and C28 submit task class ``omn19181_not_a_task_class`` through the
gateway. The gateway takes it (a bare string there); the delegate-skill consumer's closed set
refuses it, and the dispatch engine answers with a ``publisher_malformed`` terminal on the
contract's declared failure topic, ``runtime_dispatch.terminal_events.failure`` of
node_delegate_skill_orchestrator. The writer read only the delegation-failed topic, so the row
stayed ``published`` and the dying run never ended.
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

_NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"
_SKILL = yaml.safe_load(
    (_NODES / "node_delegate_skill_orchestrator" / "contract.yaml").read_text("utf-8")
)
_WRITER = yaml.safe_load(
    (_NODES / "node_gateway_workflow_terminal_effect" / "contract.yaml").read_text(
        "utf-8"
    )
)
_SKILL_FAILED = _SKILL["runtime_dispatch"]["terminal_events"]["failure"]
_SKILL_COMMAND = _SKILL["runtime_dispatch"]["command_topic"]
_TENANT = uuid4()
_CORR = uuid4()


def _terminal(**overrides: Any) -> dict[str, Any]:
    wire: dict[str, Any] = {
        "topic": _SKILL_FAILED,
        "correlation_id": str(_CORR),
        "workflow_id": None,
        "status": "failed",
        "failure_class": "publisher_malformed",
        "failure_code": "ONEX_CORE_345_ENVELOPE_VALIDATION_FAILED",
        "retryable": False,
        "failure_reason": "task_type: Input should be a declared task class",
        "origin_topic": _SKILL_COMMAND,
    }
    wire.update(overrides)
    return wire


def _make() -> tuple[HandlerGatewayWorkflowTerminal, FakeTerminalStore, FakeDeadLetter]:
    store = FakeTerminalStore([FakeRow(_TENANT, _CORR)])
    dlq = FakeDeadLetter()
    return HandlerGatewayWorkflowTerminal(store, dlq), store, dlq


def test_the_writer_subscribes_to_the_failure_topic_the_skill_contract_declares() -> (
    None
):
    assert _SKILL_FAILED in _WRITER["event_bus"]["subscribe_topics"]
    assert _SKILL_FAILED in load_contract_routes()[0]
    granted = {
        g["name"]
        for entry in _WRITER["broker_acl_grants"]
        for g in entry["grants"]
        if g.get("operation") == "READ"
    }
    assert _SKILL_FAILED in granted


async def test_a_refused_delegate_skill_command_closes_the_row_failed() -> None:
    handler, store, dlq = _make()
    result = await handler.handle(
        ModelDelegationTerminalEnvelope(topic=_SKILL_FAILED, payload=_terminal())
    )
    row = store.rows[0]
    assert result.outcome is Outcome.APPLIED
    assert row.status == "failed"
    assert dlq.records == []
    assert row.columns["failure_class"] == "PublisherMalformedError"
    assert row.columns["failure_code"] == "ONEX_CORE_345_ENVELOPE_VALIDATION_FAILED"


async def test_any_other_delegate_skill_failure_is_left_alone() -> None:
    """Known bad: only the engine's refusal is this writer's; the orchestrator's own terminals are not."""
    handler, store, dlq = _make()
    result = await handler.handle(
        ModelDelegationTerminalEnvelope(
            topic=_SKILL_FAILED,
            payload=_terminal(failure_class="ValueError", failure_code=None),
        )
    )
    assert result.outcome is Outcome.NOT_A_GATEWAY_WORKFLOW
    assert store.rows[0].status == "published"
    assert dlq.records == []


async def test_a_recorded_result_on_the_failure_topic_is_not_a_refusal() -> None:
    """Known bad: a terminal that carries a model_used is a recorded result, never the engine's refusal."""
    handler, store, dlq = _make()
    result = await handler.handle(
        ModelDelegationTerminalEnvelope(
            topic=_SKILL_FAILED, payload=_terminal(model_used="m")
        )
    )
    assert result.outcome is Outcome.NOT_A_GATEWAY_WORKFLOW
    assert store.rows[0].status == "published"
    assert dlq.records == []
