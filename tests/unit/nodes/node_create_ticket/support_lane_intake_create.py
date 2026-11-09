# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory bus and ticket core for lane intake's create path (OMN-23138).

``InMemoryTicketBus`` is the default caller: it takes the published command, runs the real ticket
write effect over a stand-in ticket core and returns the terminal on the contract's own topics.
``findings_golden`` and ``findings_error`` run one chain through a handler class and list every
way it departed from the recorded behaviour; the named mutants below each break one thing.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel

from omnimarket.handlers.handler_lane_intake_create import (
    HandlerLaneIntakeCreate,
    derive_idempotency_key,
)
from omnimarket.handlers.ticket.ticket_cli import load_ticket_topics
from omnimarket.models.model_lane_intake_create import (
    EnumLaneIntakeFailure,
    ModelLaneIntakeCreated,
    ModelLaneIntakeCreateFailed,
    ModelLaneIntakeCreateRequest,
    ModelLaneIntakeCreateResult,
    ModelLaneIntakeCreateSettings,
)
from omnimarket.models.model_ticket_io import ModelCreateTicketResult
from omnimarket.models.scm import ModelScmTerminal, ModelScmTopics
from omnimarket.models.scm.model_scm_command import ModelScmBusReceipt
from omnimarket.nodes.node_create_ticket.handlers.handler_create_ticket import (
    TicketGuardRefusedError,
)
from omnimarket.nodes.node_linear_ticket_write_effect.handlers.handler_linear_ticket_write import (
    HandlerLinearTicketWriteEffect,
)
from omnimarket.nodes.node_linear_ticket_write_effect.models.model_ticket_write_io import (
    ModelTicketWriteRequest,
)

TICKET_ID = "OMN-9001"
TICKET_URL = "https://linear.app/omninode/issue/OMN-9001"
SETTINGS = ModelLaneIntakeCreateSettings(timeout_s=5.0)
REQUEST = ModelLaneIntakeCreateRequest(
    title="a lane's ticket",
    description="Gate: x\n\n- [ ] AC1: y -- falsifier: z",
    requested_by_lane="lane-a",
    requested_by_run="run-a",
    parent="OMN-1",
)


class TicketCore:
    """Stands in for the ticket node behind the write effect; raises ``error`` when set."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[BaseModel] = []
        self.error = error

    def handle(self, command: BaseModel) -> ModelCreateTicketResult:
        self.calls.append(command)
        if self.error is not None:
            raise self.error
        return ModelCreateTicketResult(
            status="created", ticket_id=TICKET_ID, ticket_url=TICKET_URL
        )


class InMemoryTicketBus:
    """The caller of ``ProtocolTicketCommandCaller`` with no broker: one command, one terminal."""

    def __init__(self, core: TicketCore, *, answer: bool = True) -> None:
        self.core = core
        self.answer = answer
        self.published: list[tuple[str, ModelTicketWriteRequest]] = []
        self.terminal_topics: list[str] = []
        self._effect = HandlerLinearTicketWriteEffect(
            handler_factory=lambda _lane: core
        )

    async def call_topics(
        self,
        *,
        topics: ModelScmTopics,
        request: BaseModel,
        correlation_id: UUID,
        timeout_s: float,
    ) -> ModelScmTerminal:
        command = ModelTicketWriteRequest.model_validate(
            request.model_dump(mode="json")
        )
        self.published.append((topics.command, command))
        if not self.answer:
            raise TimeoutError
        result = await self._effect.handle(command)
        failed = result.outcome == "failed"
        topic = topics.failure if failed and topics.failure else topics.success
        self.terminal_topics.append(topic)
        return ModelScmTerminal(
            receipt=ModelScmBusReceipt(
                correlation_id=correlation_id,
                command_topic=topics.command,
                command_envelope_id=correlation_id,
                terminal_topic=topic,
            ),
            failed=failed,
            payload=result.model_dump(mode="json"),
        )


def build(
    handler_class: type[HandlerLaneIntakeCreate], bus: InMemoryTicketBus
) -> HandlerLaneIntakeCreate:
    return handler_class(caller=bus, topics=load_ticket_topics(), settings=SETTINGS)


async def findings_golden(handler_class: type[HandlerLaneIntakeCreate]) -> list[str]:
    """Departures from the golden chain: a create command in, the ticket out."""
    bus = InMemoryTicketBus(TicketCore())
    result = await build(handler_class, bus).handle(REQUEST)
    topics = load_ticket_topics()
    out: list[str] = []
    if len(bus.published) != 1 or bus.published[0][0] != topics.command:
        out.append("command-not-published-once-on-the-contract-topic")
    elif bus.published[0][1].operation.value != "create":
        out.append("command-is-not-a-create")
    elif bus.published[0][1].idempotency_key != derive_idempotency_key(REQUEST):
        out.append("idempotency-key-not-derived-from-the-request")
    if bus.terminal_topics != [topics.success]:
        out.append("terminal-not-on-the-success-topic")
    if not isinstance(result, ModelLaneIntakeCreated):
        out.append("result-is-not-created")
    elif (result.ticket_id, result.ticket_url) != (TICKET_ID, TICKET_URL):
        out.append("ticket-not-carried-from-the-terminal")
    elif bus.published and result.correlation_id != bus.published[0][1].correlation_id:
        out.append("result-not-correlated-to-the-command")
    return out


async def findings_error(handler_class: type[HandlerLaneIntakeCreate]) -> list[str]:
    """Departures from the error chain: the node's refusal reaches the caller as a typed failure."""
    refusal = TicketGuardRefusedError(
        "raw text: could contain credentials", ["missing_gate_line"]
    )
    bus = InMemoryTicketBus(TicketCore(error=refusal))
    result = await build(handler_class, bus).handle(REQUEST)
    topics = load_ticket_topics()
    out: list[str] = []
    if bus.terminal_topics != [topics.failure]:
        out.append("terminal-not-on-the-failure-topic")
    if not isinstance(result, ModelLaneIntakeCreateFailed):
        out.append("failure-reached-the-caller-as-something-else")
    else:
        if result.reason is not EnumLaneIntakeFailure.GUARD_REFUSED:
            out.append("reason-not-guard-refused")
        if result.finding_codes != ["missing_gate_line"]:
            out.append("finding-codes-lost")
        if "credentials" in result.model_dump_json():
            out.append("exception-text-leaked")
    silent = await build(
        handler_class, InMemoryTicketBus(TicketCore(), answer=False)
    ).handle(REQUEST)
    if not (
        isinstance(silent, ModelLaneIntakeCreateFailed)
        and silent.reason is EnumLaneIntakeFailure.NO_TERMINAL
    ):
        out.append("a-missing-terminal-is-not-a-typed-failure")
    return out


class MutantReadsFailureAsSuccess(HandlerLaneIntakeCreate):
    """Injected break (error chain): the terminal's failed flag is ignored."""

    def _read(
        self, correlation_id: UUID, terminal: ModelScmTerminal
    ) -> ModelLaneIntakeCreateResult:
        return super()._read(
            correlation_id, terminal.model_copy(update={"failed": False})
        )


class MutantDropsTheTicket(HandlerLaneIntakeCreate):
    """Injected break (golden chain): the created ticket id is replaced by the correlation id."""

    def _read(
        self, correlation_id: UUID, terminal: ModelScmTerminal
    ) -> ModelLaneIntakeCreateResult:
        got = super()._read(correlation_id, terminal)
        if isinstance(got, ModelLaneIntakeCreated):
            return got.model_copy(update={"ticket_id": str(correlation_id)})
        return got


class MutantTimeoutAsCreated(HandlerLaneIntakeCreate):
    """Injected break (error chain): a missing terminal is reported as a created ticket."""

    @staticmethod
    def _failed(
        correlation_id: UUID,
        reason: EnumLaneIntakeFailure,
        finding_codes: list[str] | None = None,
    ) -> ModelLaneIntakeCreateResult:
        if reason is EnumLaneIntakeFailure.NO_TERMINAL:
            return ModelLaneIntakeCreated(
                correlation_id=correlation_id, ticket_id="none", ticket_url=""
            )
        return HandlerLaneIntakeCreate._failed(correlation_id, reason, finding_codes)
