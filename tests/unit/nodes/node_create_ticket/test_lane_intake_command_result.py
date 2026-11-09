# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23138: lane intake opens a ticket by command and reads a typed result.

Recorded cases: the create the ticket write node admits (with and without the caller's own
idempotency key, and its replay), and each refusal the node terminates with; every one reaches
the caller as ``ModelLaneIntakeCreateFailed`` with the node's reason and the guard's codes. The
chain tests run the golden and the error chain through the real write effect, then show each
named mutant turns the matching chain red.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import JsonValue

from omnimarket.handlers.handler_lane_intake_create import (
    HandlerLaneIntakeCreate,
    derive_idempotency_key,
)
from omnimarket.inference.secret_store_resolver import SecretStoreConfigurationError
from omnimarket.models.model_lane_intake_create import (
    EnumLaneIntakeFailure,
    ModelLaneIntakeCreated,
    ModelLaneIntakeCreateFailed,
)
from omnimarket.models.scm import ModelScmTerminal
from omnimarket.models.scm.model_scm_command import ModelScmBusReceipt
from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    TicketGuardRefusedError,
    TicketGuardUnavailableError,
)
from tests.unit.nodes.node_create_ticket.support_lane_intake_create import (
    REQUEST,
    TICKET_ID,
    InMemoryTicketBus,
    MutantDropsTheTicket,
    MutantReadsFailureAsSuccess,
    MutantTimeoutAsCreated,
    TicketCore,
    build,
    findings_error,
    findings_golden,
)

pytestmark = pytest.mark.unit


async def test_lane_intake_command_result_creates_the_ticket_by_command() -> None:
    bus = InMemoryTicketBus(TicketCore())
    result = await build(HandlerLaneIntakeCreate, bus).handle(REQUEST)
    assert isinstance(result, ModelLaneIntakeCreated)
    assert result.ticket_id == TICKET_ID
    assert len(bus.core.calls) == 1
    sent = bus.published[0][1]
    assert (sent.operation.value, sent.requested_by_lane, sent.parent) == (
        "create",
        "lane-a",
        "OMN-1",
    )


async def test_lane_intake_command_result_a_retry_replays_and_creates_once() -> None:
    bus = InMemoryTicketBus(TicketCore())
    handler = build(HandlerLaneIntakeCreate, bus)
    first = await handler.handle(REQUEST)
    again = await handler.handle(REQUEST)
    assert isinstance(first, ModelLaneIntakeCreated)
    assert isinstance(again, ModelLaneIntakeCreated)
    assert again.replayed is True
    assert len(bus.core.calls) == 1


async def test_lane_intake_command_result_the_callers_key_wins_over_the_derived_one() -> (
    None
):
    keyed = REQUEST.model_copy(update={"idempotency_key": "mine"})
    bus = InMemoryTicketBus(TicketCore())
    await build(HandlerLaneIntakeCreate, bus).handle(keyed)
    assert bus.published[0][1].idempotency_key == "mine"
    assert derive_idempotency_key(REQUEST) != "mine"
    other = REQUEST.model_copy(update={"title": "another"})
    assert derive_idempotency_key(other) != derive_idempotency_key(REQUEST)


@pytest.mark.parametrize(
    ("error", "reason", "codes"),
    [
        (
            TicketGuardRefusedError("no", ["unfalsified_criterion"]),
            EnumLaneIntakeFailure.GUARD_REFUSED,
            ["unfalsified_criterion"],
        ),
        (
            TicketGuardUnavailableError("missing"),
            EnumLaneIntakeFailure.GUARD_UNAVAILABLE,
            [],
        ),
        (
            SecretStoreConfigurationError("no key"),
            EnumLaneIntakeFailure.SECRET_UNAVAILABLE,
            ["linear_api_key_unresolved"],
        ),
        (RuntimeError("boom"), EnumLaneIntakeFailure.OPERATION_FAILED, []),
    ],
)
async def test_lane_intake_command_result_a_typed_refusal_reaches_the_caller_as_a_failure(
    error: Exception, reason: EnumLaneIntakeFailure, codes: list[str]
) -> None:
    bus = InMemoryTicketBus(TicketCore(error=error))
    result = await build(HandlerLaneIntakeCreate, bus).handle(REQUEST)
    assert isinstance(result, ModelLaneIntakeCreateFailed)
    assert (result.reason, result.finding_codes) == (reason, codes)
    assert "boom" not in result.model_dump_json()


def _terminal(*, failed: bool, payload: dict[str, JsonValue]) -> ModelScmTerminal:
    cid = uuid4()
    return ModelScmTerminal(
        receipt=ModelScmBusReceipt(
            correlation_id=cid,
            command_topic="command",
            command_envelope_id=cid,
            terminal_topic="terminal",
        ),
        failed=failed,
        payload=payload,
    )


@pytest.mark.parametrize("failed", [False, True])
def test_lane_intake_command_result_an_unreadable_terminal_is_a_typed_failure(
    failed: bool,
) -> None:
    handler = build(HandlerLaneIntakeCreate, InMemoryTicketBus(TicketCore()))
    got = handler._read(uuid4(), _terminal(failed=failed, payload={"x": "y"}))
    assert isinstance(got, ModelLaneIntakeCreateFailed)
    assert got.reason is EnumLaneIntakeFailure.BAD_TERMINAL


async def test_lane_intake_command_result_golden_chain_event_reads_clean() -> None:
    assert await findings_golden(HandlerLaneIntakeCreate) == []


async def test_lane_intake_command_result_error_chain_event_reads_clean() -> None:
    assert await findings_error(HandlerLaneIntakeCreate) == []


async def test_chain_golden_mutation_drops_the_ticket_turns_the_golden_chain_red() -> (
    None
):
    assert await findings_golden(MutantDropsTheTicket) == [
        "ticket-not-carried-from-the-terminal"
    ]


async def test_chain_error_mutation_reads_failure_as_success_turns_the_error_chain_red() -> (
    None
):
    found = await findings_error(MutantReadsFailureAsSuccess)
    assert found == ["reason-not-guard-refused", "finding-codes-lost"]


async def test_chain_error_mutation_timeout_as_created_turns_the_error_chain_red() -> (
    None
):
    assert await findings_error(MutantTimeoutAsCreated) == [
        "a-missing-terminal-is-not-a-typed-failure"
    ]
