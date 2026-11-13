# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam between the scheduled ticket closer and the ticket write node (OMN-24301).

Three crossings, each failing when either side changes shape: the reader reads the ticket write node's own
completed and failed answer models (not a hand-written dict), the closer's contract holds the broker grants
for exactly the topics the ticket call uses, and the ticket call's topics are the write node's declared ones.
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
CLOSER_CONTRACT = NODES / "node_ticket_closer_scheduled_effect/contract.yaml"
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


def grants_of(contract: Path) -> set[tuple[str, str]]:
    loaded = yaml.safe_load(contract.read_text(encoding="utf-8"))
    return {
        (grant["name"], grant["operation"])
        for entry in loaded["broker_acl_grants"]
        for grant in entry["grants"]
    }


def missing_grants(held: set[tuple[str, str]]) -> set[tuple[str, str]]:
    topics = load_ticket_topics()
    assert topics.failure is not None
    needed = {
        (topics.command, "WRITE"),
        (topics.command, "DESCRIBE"),
        (topics.success, "READ"),
        (topics.success, "DESCRIBE"),
        (topics.failure, "READ"),
        (topics.failure, "DESCRIBE"),
    }
    return needed - held


def test_the_closer_contract_grants_the_ticket_topics_the_call_uses() -> None:
    assert missing_grants(grants_of(CLOSER_CONTRACT)) == set()


def test_the_calls_topics_are_the_write_nodes_declared_ones() -> None:
    dispatch = yaml.safe_load(WRITE_CONTRACT.read_text(encoding="utf-8"))[
        "runtime_dispatch"
    ]
    topics = load_ticket_topics()
    assert topics.command == dispatch["command_topic"]
    assert topics.success == dispatch["terminal_events"]["success"]
    assert topics.failure == dispatch["terminal_events"]["failure"]


def test_failing_control_a_contract_without_the_command_grant_is_caught() -> None:
    held = grants_of(CLOSER_CONTRACT) - {(load_ticket_topics().command, "WRITE")}
    assert missing_grants(held) == {(load_ticket_topics().command, "WRITE")}
