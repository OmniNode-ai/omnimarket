# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ticket write node as the ticket closer's writer, and the seam it binds (OMN-24138)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from pydantic import JsonValue

from omnimarket.events.ticket_write_io import (
    ModelTicketWriteRequest,
)
from omnimarket.handlers.ticket.ticket_cli import load_ticket_topics
from omnimarket.models.scm import ModelScmBusReceipt, ModelScmTerminal
from omnimarket.models.ticket_closer import ModelCloserWriteReceipt
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.ticket_done_writer import (
    BusTicketDoneWriter,
)

pytestmark = pytest.mark.unit

WRITE_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_linear_ticket_write_effect/contract.yaml"
)


def terminal(
    *, failed: bool = False, payload: dict[str, JsonValue] | None = None
) -> ModelScmTerminal:
    topics = load_ticket_topics()
    return ModelScmTerminal(
        receipt=ModelScmBusReceipt(
            correlation_id=uuid4(),
            command_topic=topics.command,
            command_envelope_id=uuid4(),
            terminal_topic=topics.success,
        ),
        failed=failed,
        payload=payload or {},
    )


class Call:
    def __init__(
        self,
        answers: list[ModelScmTerminal] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.answers = answers or []
        self.raises = raises
        self.requests: list[ModelTicketWriteRequest] = []

    def __call__(self, request: ModelTicketWriteRequest) -> ModelScmTerminal:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        return self.answers.pop(0) if self.answers else terminal()


def mark(call: Call, ticket_id: str = "OMN-1") -> ModelCloserWriteReceipt:
    return BusTicketDoneWriter(call).mark_done(
        ticket_id,
        state="Done",
        lane="ticket-closer-fixture",
        evidence="dod_verify total=2",
    )


def test_golden_comments_the_evidence_then_transitions() -> None:
    call = Call()
    receipt = mark(call)
    assert receipt.ok is True
    assert [r.operation.value for r in call.requests] == ["comment", "transition"]
    comment, transition = call.requests
    assert comment.ticket_id == transition.ticket_id == "OMN-1"
    assert comment.body.startswith("actor: ticket-closer-fixture (node)\n")
    assert "dod_verify total=2" in comment.body
    assert transition.state == "Done"
    assert transition.requested_by_lane == "ticket-closer-fixture"


def test_error_a_failed_comment_prevents_the_transition() -> None:
    call = Call([terminal(failed=True, payload={"reason": "no such ticket"})])
    receipt = mark(call)
    assert receipt.ok is False
    assert receipt.reason == "no such ticket"
    assert [r.operation.value for r in call.requests] == ["comment"]


def test_error_a_failed_transition_carries_the_refusal_and_its_codes() -> None:
    call = Call(
        [
            terminal(),
            terminal(
                failed=True,
                payload={"reason": "done guard", "finding_codes": ["NO_RECEIPT"]},
            ),
        ]
    )
    receipt = mark(call)
    assert receipt.ok is False
    assert receipt.reason == "done guard [NO_RECEIPT]"


def test_error_a_call_that_times_out_is_reported() -> None:
    receipt = mark(Call(raises=TimeoutError("no terminal")))
    assert receipt.ok is False
    assert receipt.reason == "comment write raised TimeoutError"


def test_a_retried_decision_replays_and_another_ticket_does_not() -> None:
    first, second, other = Call(), Call(), Call()
    mark(first)
    mark(second)
    mark(other, "OMN-2")
    assert [r.idempotency_key for r in first.requests] == [
        r.idempotency_key for r in second.requests
    ]
    assert first.requests[0].idempotency_key != first.requests[1].idempotency_key
    assert first.requests[0].idempotency_key != other.requests[0].idempotency_key


def test_seam_the_bound_topics_are_the_ticket_write_nodes_declared_pair() -> None:
    """The adapter's call binds ``ticket_cli``'s topics; the node must subscribe to the command and publish both terminals."""
    loaded = yaml.safe_load(WRITE_CONTRACT.read_text(encoding="utf-8"))
    topics = load_ticket_topics()
    assert topics.command in loaded["event_bus"]["subscribe_topics"]
    assert topics.success in loaded["event_bus"]["publish_topics"]
    assert topics.failure in loaded["event_bus"]["publish_topics"]
