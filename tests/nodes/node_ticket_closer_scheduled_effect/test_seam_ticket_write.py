# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam between the scheduled ticket closer and the ticket write node (OMN-24301).

Three crossings, each failing when either side changes shape: the reader reads the ticket write node's own
completed and failed answer models (not a hand-written dict), the lane login's broker rows (spelled here,
declared in the overlays repository) are exactly the topics the ticket call uses, and the ticket call's topics are the write node's declared ones.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
import yaml

from omnimarket.handlers.ticket.ticket_cli import load_ticket_topics
from omnimarket.models.model_ticket_io import (
    EnumTicketOperation,
    ModelCreateTicketResult,
)
from omnimarket.models.scm import ModelScmBusReceipt, ModelScmTerminal
from omnimarket.models.ticket_write_io import (
    ModelTicketWriteCompleted,
    ModelTicketWriteFailed,
    ModelTicketWriteRequest,
)
from omnimarket.nodes.node_ticket_closer_scheduled_effect.handlers.ticket_reader import (
    BusTicketReader,
)

pytestmark = pytest.mark.unit

NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
WRITE_CONTRACT = NODES / "node_linear_ticket_write_effect/contract.yaml"
BODY = "- [ ] AC1: it works -- falsifier: a test\n"


def terminal_of(
    model: ModelTicketWriteCompleted | ModelTicketWriteFailed,
) -> ModelScmTerminal:
    topics = load_ticket_topics()
    failed = isinstance(model, ModelTicketWriteFailed)
    return ModelScmTerminal(
        receipt=ModelScmBusReceipt(
            correlation_id=model.correlation_id,
            command_topic=topics.command,
            command_envelope_id=uuid4(),
            terminal_topic=topics.failure
            if failed and topics.failure
            else topics.success,
        ),
        failed=failed,
        payload=model.model_dump(mode="json"),
    )


def answer(call_result: ModelScmTerminal):
    def call(request: ModelTicketWriteRequest) -> ModelScmTerminal:
        assert request.operation is EnumTicketOperation.READ
        return call_result

    return call


def test_the_reader_reads_the_write_nodes_own_completed_model() -> None:
    completed = ModelTicketWriteCompleted(
        correlation_id=uuid4(),
        operation=EnumTicketOperation.READ,
        idempotency_key="k",
        requested_by_lane="l",
        result=ModelCreateTicketResult(
            status="read",
            operation=EnumTicketOperation.READ,
            ticket_id="OMN-1",
            ticket_status="In Progress",
            description_body=BODY,
        ),
    )
    reader = BusTicketReader(answer(terminal_of(completed)), lane="l")
    assert reader.read("OMN-1") == ("In Progress", BODY)


def test_the_reader_reads_the_write_nodes_own_failed_model_as_unread() -> None:
    failed = ModelTicketWriteFailed(
        correlation_id=uuid4(),
        operation=EnumTicketOperation.READ,
        idempotency_key="k",
        requested_by_lane="l",
        reason="no such ticket",
        terminal_failure_cause="not_found",
    )
    reader = BusTicketReader(answer(terminal_of(failed)), lane="l")
    assert reader.read("OMN-1") is None


# The rows the lane bus login needs for the ticket call, as the overlays repository declares them in the scope
# broker_acls_ticket_closer_dev_runtime (the node-side ticket calls ride the declared lane's login, not the runtime role
# whose rows the contract derives from its event_bus topics).
LANE_LOGIN_ROWS = {
    ("onex.cmd.omnimarket.linear-ticket-write-requested.v1", "WRITE"),
    ("onex.cmd.omnimarket.linear-ticket-write-requested.v1", "DESCRIBE"),
    ("onex.evt.omnimarket.linear-ticket-write-completed.v1", "READ"),
    ("onex.evt.omnimarket.linear-ticket-write-completed.v1", "DESCRIBE"),
    ("onex.evt.omnimarket.linear-ticket-write-failed.v1", "READ"),
    ("onex.evt.omnimarket.linear-ticket-write-failed.v1", "DESCRIBE"),
}


def rows_the_call_needs(
    command: str, success: str, failure: str
) -> set[tuple[str, str]]:
    return {
        (command, "WRITE"),
        (command, "DESCRIBE"),
        (success, "READ"),
        (success, "DESCRIBE"),
        (failure, "READ"),
        (failure, "DESCRIBE"),
    }


def test_the_lane_login_rows_are_the_topics_the_call_uses() -> None:
    topics = load_ticket_topics()
    assert topics.failure is not None
    assert (
        rows_the_call_needs(topics.command, topics.success, topics.failure)
        == LANE_LOGIN_ROWS
    )


def test_the_calls_topics_are_the_write_nodes_declared_ones() -> None:
    dispatch = yaml.safe_load(WRITE_CONTRACT.read_text(encoding="utf-8"))[
        "runtime_dispatch"
    ]
    topics = load_ticket_topics()
    assert topics.command == dispatch["command_topic"]
    assert topics.success == dispatch["terminal_events"]["success"]
    assert topics.failure == dispatch["terminal_events"]["failure"]


def test_failing_control_a_renamed_command_topic_is_caught() -> None:
    topics = load_ticket_topics()
    assert topics.failure is not None
    renamed = rows_the_call_needs(
        "onex.cmd.omnimarket.linear-ticket-write-renamed.v1",
        topics.success,
        topics.failure,
    )
    assert renamed != LANE_LOGIN_ROWS
