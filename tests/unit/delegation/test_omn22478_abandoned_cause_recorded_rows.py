# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The completion-bound terminal names what the row records (OMN-22478).

Both fixtures are durable workflow rows read from the dev lane on 2026-11-06,
with the request text and the credential reference scrubbed:

* ``c490e90f``: submitted 2026-11-04, three attempts (two customer-route
  retries after a credential failure), then 20 rejected redeliveries of the
  superseded attempts' answers, the last at 2026-11-06T15:49:22Z.
* ``75c15093``: submitted 2026-11-06T16:28:29Z. The inference effect answered
  in 1.8 s and published the response, but the orchestrator never consumed it;
  the routing decision was redelivered at least five times meanwhile.

The completion bound closed runs like both with the text "the runtime process
holding its in-flight leg went away", which no process did. These tests fail
on that text and pass when the terminal states the attempt in flight and the
superseded answers it rejected. The redelivery test pins that a redelivered
routing decision for a run with an attempt in flight opens no new attempt.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from omnibase_core.models.delegation.wire.model_delegation_failed import (
    ModelDelegationFailed,
)

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    DelegationWorkflowState,
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.state_codec import (
    StateIoCodec,
    decode,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "omn22478_abandoned_rows"


def _row(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((_FIXTURES / name).read_text())
    return loaded


def _terminal_reason(row: dict[str, Any]) -> str:
    built = StateIoCodec().build_abandoned_terminal(
        correlation_id=row["payload"]["correlation_id"],
        tenant_id=row["tenant_id"],
        state=row["payload"]["state"],
        payload_json=json.dumps(row["payload"]),
        failure_class=EnumDelegationFailureClass.RUNTIME_RESTART_DURING_DELEGATION.value,
        failure_code="ONEX_MARKET_DELEGATION_RUNTIME_RESTART",
        max_wall_seconds=900,
    )
    assert built is not None
    terminal = ModelDelegationFailed.model_validate(built[2])
    return terminal.failure_reason


@pytest.mark.unit
def test_superseded_rejections_are_named_not_a_process_that_went_away() -> None:
    reason = _terminal_reason(_row("c490e90f_superseded_rejections.json"))
    assert "went away" not in reason
    assert "inference attempt 3 (649b9b71-3ceb-5b11-8a2d-89c22c6c9e4a)" in reason
    assert "tenant_overlay/claude-sonnet-5-5" in reason
    assert "20 redelivered responses of superseded attempts" in reason
    assert "2026-11-06T15:49:22.764822+00:00" in reason
    assert "35a80efc-b431-53f7-a7b8-987482bc6d64" in reason
    assert "900s" in reason


@pytest.mark.unit
def test_an_answer_never_consumed_names_the_attempt_in_flight() -> None:
    reason = _terminal_reason(_row("75c15093_response_never_consumed.json"))
    assert "went away" not in reason
    assert "inference attempt 1 (" in reason
    assert "cheap_frontier/cohere/north-mini-code:free" in reason
    assert "superseded" not in reason


@pytest.mark.unit
def test_a_redelivered_routing_decision_opens_no_new_attempt() -> None:
    row = _row("75c15093_response_never_consumed.json")
    workflow: DelegationWorkflowState = decode(json.dumps(row["payload"]))
    assert workflow.inference_intent_in_flight is True
    assert workflow.routing_decision is not None
    attempt = workflow.current_inference_attempt_id
    cid = UUID(row["payload"]["correlation_id"])
    handler = HandlerDelegationWorkflow(workflows={cid: workflow})
    redelivered = workflow.routing_decision.model_copy()
    for _ in range(5):
        assert handler.handle_routing_decision(redelivered) == []
    assert workflow.inference_attempt_ordinal == 1
    assert workflow.current_inference_attempt_id == attempt
