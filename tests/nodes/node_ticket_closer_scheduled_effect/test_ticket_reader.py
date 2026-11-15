# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ticket write node as the scheduled closer's reader: state and description (OMN-24301)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import JsonValue

from omnimarket.handlers.ticket.ticket_cli import load_ticket_topics
from omnimarket.models.scm import ModelScmBusReceipt, ModelScmTerminal
from omnimarket.models.ticket_write_io import (
    ModelTicketWriteRequest,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.ticket_reader import (
    BusTicketReader,
)

pytestmark = pytest.mark.unit

LANE = "ticket-closer-fixture"
BODY = "- [ ] AC1: it works -- falsifier: a test\n"
READING = {"ticket_status": "In Progress", "description_body": BODY}


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


def reader(call: Call) -> BusTicketReader:
    return BusTicketReader(call, lane=LANE)


def test_golden_a_completed_read_returns_the_state_and_the_description() -> None:
    call = Call([terminal(payload={"result": READING})])
    found = reader(call).read("OMN-1")
    assert found == ("In Progress", BODY)
    sent = call.requests[0]
    assert sent.operation.value == "read"
    assert sent.ticket_id == "OMN-1"
    assert sent.requested_by_lane == LANE


def test_each_read_carries_its_own_key_so_the_answer_is_not_replayed() -> None:
    call = Call(
        [terminal(payload={"result": READING}), terminal(payload={"result": READING})]
    )
    found = reader(call)
    assert found.read("OMN-1") == found.read("OMN-1") == ("In Progress", BODY)
    assert call.requests[0].idempotency_key != call.requests[1].idempotency_key
    assert call.requests[0].correlation_id != call.requests[1].correlation_id


def test_error_chain_a_failed_terminal_is_not_a_reading() -> None:
    call = Call([terminal(failed=True, payload={"reason": "no such ticket"})])
    assert reader(call).read("OMN-404") is None


def test_error_chain_a_raising_call_is_not_a_reading() -> None:
    assert reader(Call(raises=TimeoutError("no terminal"))).read("OMN-1") is None


def test_error_chain_a_result_without_a_state_is_not_a_reading() -> None:
    answers = [
        terminal(payload={"result": {**READING, "ticket_status": ""}}),
        terminal(payload={"result": {"description_body": BODY}}),
        terminal(payload={}),
    ]
    for answer in answers:
        assert reader(Call([answer])).read("OMN-1") is None


def test_failing_control_an_empty_answer_fails_the_none_assertion() -> None:
    """The injected break: a reader that answered ("", "") for an unreadable ticket would be
    caught by the None assertions above — an empty state reads as an answer, not as unread."""

    class Broken:
        def read(self, ticket_id: str) -> tuple[str, str] | None:
            return ("", "")

    with pytest.raises(AssertionError):
        assert Broken().read("OMN-1") is None
