# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A publisher-malformed command's terminal closes its gateway row (OMN-26566).

The dispatch engine answers a command it refused as ``publisher_malformed`` with a typed
terminal on the contract's declared failure topic. It carries the correlation id and the
refusal, and nothing else: no tenant, no model, no metrics. The writer dead-lettered it as
``missing_model_used``, so the row stayed ``published`` and C16's dying run never ended.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

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

_ROUTES, _ = load_contract_routes()
_FAILED = next(t for t, s in _ROUTES.items() if s == "failed")
_COMPLETED = next(t for t, s in _ROUTES.items() if s == "completed")
_TENANT = uuid4()
_CORR = uuid4()


def _terminal(**overrides: Any) -> dict[str, Any]:
    wire: dict[str, Any] = {
        "topic": _FAILED,
        "correlation_id": str(_CORR),
        "workflow_id": None,
        "status": "failed",
        "failure_class": "publisher_malformed",
        "failure_code": None,
        "retryable": False,
        "failure_reason": "task_type: Input should be a valid task class",
        "origin_topic": "onex.cmd.omnibase-infra.delegation-request.v1",
    }
    wire.update(overrides)
    return wire


def _make(
    rows: list[FakeRow] | None = None,
) -> tuple[HandlerGatewayWorkflowTerminal, FakeTerminalStore, FakeDeadLetter]:
    store = FakeTerminalStore(rows if rows is not None else [FakeRow(_TENANT, _CORR)])
    dlq = FakeDeadLetter()
    return HandlerGatewayWorkflowTerminal(store, dlq), store, dlq


async def test_a_malformed_command_terminal_closes_the_row_failed() -> None:
    handler, store, dlq = _make()
    result = await handler.handle(
        ModelDelegationTerminalEnvelope(topic=_FAILED, payload=_terminal())
    )
    row = store.rows[0]
    assert result.outcome is Outcome.APPLIED
    assert result.workflow_id == row.workflow_id
    assert row.status == "failed"
    assert dlq.records == []
    assert row.columns["model_used"] == "none"
    assert row.columns["total_tokens"] == 0
    assert row.columns["failure_class"] == "PublisherMalformedError"
    assert row.columns["failure_code"] == "ONEX_CORE_345_ENVELOPE_VALIDATION_FAILED"


async def test_a_stated_failure_code_is_kept() -> None:
    handler, store, _ = _make()
    await handler.handle(
        ModelDelegationTerminalEnvelope(
            topic=_FAILED, payload=_terminal(failure_code="ONEX_CORE_001_X")
        )
    )
    assert store.rows[0].columns["failure_code"] == "ONEX_CORE_001_X"


async def test_an_unknown_correlation_is_not_a_gateway_workflow() -> None:
    handler, _store, dlq = _make(rows=[])
    result = await handler.handle(
        ModelDelegationTerminalEnvelope(topic=_FAILED, payload=_terminal())
    )
    assert result.outcome is Outcome.NOT_A_GATEWAY_WORKFLOW
    assert dlq.records == []


async def test_a_replayed_malformed_terminal_changes_nothing() -> None:
    handler, _store, _ = _make()
    envelope = ModelDelegationTerminalEnvelope(topic=_FAILED, payload=_terminal())
    await handler.handle(envelope)
    result = await handler.handle(envelope)
    assert result.outcome is Outcome.ALREADY_TERMINAL


async def test_a_terminal_that_is_not_publisher_malformed_still_needs_a_model() -> None:
    """Known bad: the exception is exactly the one failure class, never a missing model."""
    handler, store, dlq = _make()
    result = await handler.handle(
        ModelDelegationTerminalEnvelope(
            topic=_FAILED, payload=_terminal(failure_class="no_dispatcher")
        )
    )
    assert result.outcome is Outcome.DEAD_LETTERED
    assert store.rows[0].status == "published"
    assert len(dlq.records) == 1


async def test_a_completed_topic_never_takes_the_exception() -> None:
    handler, store, _dlq = _make()
    result = await handler.handle(
        ModelDelegationTerminalEnvelope(topic=_COMPLETED, payload=_terminal())
    )
    assert result.outcome is Outcome.DEAD_LETTERED
    assert store.rows[0].status == "published"


async def test_a_two_row_correlation_is_ambiguous_not_guessed() -> None:
    handler, store, _dlq = _make(
        rows=[FakeRow(_TENANT, _CORR), FakeRow(uuid4(), _CORR)]
    )
    result = await handler.handle(
        ModelDelegationTerminalEnvelope(topic=_FAILED, payload=_terminal())
    )
    assert result.outcome is Outcome.DEAD_LETTERED
    assert all(r.status == "published" for r in store.rows)
