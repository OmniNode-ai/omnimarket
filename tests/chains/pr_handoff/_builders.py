# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Drive the real PR handoff workflow over an in-memory bus (OMN-20636).

Each case runs the real node_pr_handoff_orchestrator handler (with the real
decision compute in process and the in-memory row store under compare-and-set)
and the real node_pr_handoff_ledger_effect handler. Only the ledger host is
scripted: :class:`ScriptedLedger` answers each append the way `onex work-ledger
serve` does (accepted, duplicate, refused, error) or not at all (no receipt in
time). Every message is published on its contract topic of an in-memory bus,
in the order the runtime would publish it, and recorded for the chain
assertion.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from pydantic import BaseModel

from omnimarket.events.topics import (
    PR_HANDOFF_LEDGER_APPENDED_TOPIC_V1,
    PR_HANDOFF_REQUESTED_TOPIC_V1,
    PR_STATE_OBSERVED_TOPIC_V1,
)
from omnimarket.models.pr_handoff import (
    EnumPrHandoffLabProofSource,
    ModelPrHandoffLabProof,
    ModelPrHandoffLedgerAppendCommand,
    ModelPrHandoffLedgerAppended,
    ModelPrHandoffRequested,
)
from omnimarket.models.work_ledger_append import (
    EnumWorkLedgerAppendStatus,
    ModelWorkLedgerAppendReceipt,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_pr_handoff_ledger_effect.handlers.handler_pr_handoff_ledger_effect import (
    HandlerPrHandoffLedgerEffect,
)
from omnimarket.nodes.node_pr_handoff_orchestrator.event_topics import publish_topic_for
from omnimarket.nodes.node_pr_handoff_orchestrator.handlers.handler_pr_handoff_orchestrator import (
    HandlerPrHandoffOrchestrator,
)
from omnimarket.nodes.node_pr_handoff_orchestrator.models.model_pr_handoff_observation_ingress import (
    ModelPrHandoffObservationIngress,
)
from omnimarket.nodes.node_pr_handoff_orchestrator.orchestration.core import (
    PrHandoffMessage,
)
from omnimarket.nodes.node_pr_handoff_orchestrator.orchestration.row_store import (
    InMemoryPrHandoffRowStore,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers.handler_work_ledger_append_effect import (
    HandlerWorkLedgerAppendEffect,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    ModelAppendCommandResult,
)
from tests.chains.chain_assert import ChainEvent, ChainRecorder

REPO = "omnimarket"
PR = 4242
KEY = f"{REPO}#{PR}"
TICKET = "OMN-20636"
LANE = "lab-lane-9143"
HEAD = "d393be422bd17f30597b4b97ff8f43e12ee68558"
OTHER_HEAD = "0caed7692c2c51a1e8b5f0e3c2d4a6b8c0e1f2a3"
T0 = datetime(2026, 10, 5, 19, 0, 0, tzinfo=UTC)

# Ingress topics, from the subscribing contracts; the orchestrator's own
# emissions go on the topics its contract maps them to (publish_topic_for).
_INGRESS_TOPICS: dict[type[BaseModel], str] = {
    ModelPrHandoffRequested: PR_HANDOFF_REQUESTED_TOPIC_V1,
    ModelPrHandoffObservationIngress: PR_STATE_OBSERVED_TOPIC_V1,
    ModelPrHandoffLedgerAppended: PR_HANDOFF_LEDGER_APPENDED_TOPIC_V1,
}


_INGRESS = (
    ModelPrHandoffRequested,
    ModelPrHandoffObservationIngress,
    ModelPrHandoffLedgerAppended,
)


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def lab_comment(head: str = HEAD) -> ModelPrHandoffLabProof:
    return ModelPrHandoffLabProof(
        source=EnumPrHandoffLabProofSource.COMMENT,
        line=f"Lab: head={head} host=h202 lane={LANE} command=uv run pytest tests/chains/pr_handoff observed=13 passed",
    )


def request(cid: UUID, **overrides: Any) -> ModelPrHandoffRequested:
    data: dict[str, Any] = {
        "correlation_id": cid,
        "repo": REPO,
        "pr_number": PR,
        "expected_head_sha": HEAD[:7],
        "lane": LANE,
        "claim_tickets": (TICKET,),
        "body_ticket_ids": (TICKET,),
        "lab_proof": lab_comment(),
        "delegation": "delegated=0 delegation_reason=no-text-or-code",
        "requesting_host": "h202",
        "requested_at": at(0),
    }
    data.update(overrides)
    return ModelPrHandoffRequested(**data)


def observation(
    observed: datetime, **overrides: Any
) -> ModelPrHandoffObservationIngress:
    """The PR watcher's wire payload for the PR, as node_pr_state_emit_effect publishes it."""
    data: dict[str, Any] = {
        "repo": REPO,
        "pr_number": PR,
        "state": "open",
        "head_sha": HEAD,
        "base": "dev",
        "head_ref": "jonah/omn-20636-pr-handoff-bus",
        "title": f"feat({TICKET}): PR handoff over the bus",
        "draft": False,
        "author": "lane-author",
        "author_is_bot": False,
        "labels": [],
        "armed": False,
        "queued": False,
        "watcher_class": "ready",
        "ci_verdict": "PENDING",
        "red_contexts": [],
        "pending_contexts": ["CI Summary"],
        "ci_read_at": stamp(observed),
        "merged_at": "",
        "observed_at": stamp(observed),
    }
    data.update(overrides)
    return ModelPrHandoffObservationIngress.model_validate(data)


@dataclass
class ScriptedLedger:
    """The ledger host: one scripted answer per append; None is no receipt in time."""

    answers: list[EnumWorkLedgerAppendStatus | None]
    requests: list[ModelWorkLedgerAppendRequest] = field(default_factory=list)

    async def append(
        self, request: ModelWorkLedgerAppendRequest, *, timeout_s: float
    ) -> ModelWorkLedgerAppendReceipt | None:
        del timeout_s
        self.requests.append(request)
        status = self.answers.pop(0)
        if status is None:
            return None
        accepted = status in (
            EnumWorkLedgerAppendStatus.ACCEPTED,
            EnumWorkLedgerAppendStatus.DUPLICATE,
        )
        return ModelWorkLedgerAppendReceipt(
            request_id=request.request_id,
            status=status,
            exit_code=0 if accepted else 65,
            message="appended"
            if accepted
            else "row 2 (TERMINAL) refused by the ledger grammar",
            ledger_lines=[47200, 47201]
            if status is EnumWorkLedgerAppendStatus.ACCEPTED
            else [],
            ledger_host="h200",
            duration_ms=40,
        )


@dataclass
class InMemoryLedgerFile:
    """The ledger of record for a chain: the real append handler writes here."""

    text: str = ""

    def append(self, rows: str) -> ModelAppendCommandResult:
        self.text += rows if rows.endswith("\n") else rows + "\n"
        return ModelAppendCommandResult(exit_code=0)

    def read_text(self) -> str:
        return self.text


@dataclass
class RealLedger:
    """node_work_ledger_append_effect's real handler on an in-memory ledger.

    ``lost`` receipts are dropped after the handler appended, the way a receipt
    lost on the bus would be; the append itself happens.
    """

    lost: int = 0
    ledger: InMemoryLedgerFile = field(default_factory=InMemoryLedgerFile)
    requests: list[ModelWorkLedgerAppendRequest] = field(default_factory=list)
    signing_key: Ed25519PrivateKey = field(
        default_factory=Ed25519PrivateKey.generate, repr=False
    )

    async def append(
        self, request: ModelWorkLedgerAppendRequest, *, timeout_s: float
    ) -> ModelWorkLedgerAppendReceipt | None:
        del timeout_s
        self.requests.append(request)
        receipt = HandlerWorkLedgerAppendEffect(
            runner=self.ledger,
            reader=self.ledger,
            host_name="h200",
            public_keys={"chain-test": self.signing_key.public_key()},
        ).handle(request.signed("chain-test", self.signing_key))
        if self.lost:
            self.lost -= 1
            return None
        return receipt


@dataclass
class HandoffRun:
    """One workflow over one in-memory bus; ``events`` in publication order."""

    ledger: ScriptedLedger | RealLedger
    bus: EventBusInmemory = field(
        default_factory=lambda: EventBusInmemory(
            environment="test", group="pr-handoff-chain"
        )
    )
    store: InMemoryPrHandoffRowStore = field(default_factory=InMemoryPrHandoffRowStore)
    recorder: ChainRecorder = field(init=False)
    orchestrator: HandlerPrHandoffOrchestrator = field(init=False)
    effect: HandlerPrHandoffLedgerEffect = field(init=False)
    effect_clock: datetime = T0

    def __post_init__(self) -> None:
        self.recorder = ChainRecorder(self.bus)
        self.orchestrator = HandlerPrHandoffOrchestrator(store=self.store)
        self.effect = HandlerPrHandoffLedgerEffect(
            appender=self.ledger, host_name="h202", clock=lambda: self.effect_clock
        )

    @property
    def events(self) -> tuple[ChainEvent, ...]:
        return self.recorder.events

    def events_for(self, cid: UUID) -> tuple[ChainEvent, ...]:
        return tuple(e for e in self.recorder.events if e.correlation_id == cid)

    async def bus_history_count(self) -> int:
        return await self.recorder.bus_history_count()

    async def _publish(self, message: BaseModel, cid: UUID) -> None:
        topic = _INGRESS_TOPICS.get(type(message)) or publish_topic_for(message)
        await self.recorder.publish(topic, message, correlation_id=cid)

    async def send(self, message: BaseModel, *, cid: UUID) -> None:
        """Publish ``message`` and run everything it causes to quiescence.

        An observation carries no correlation id of its own; ``cid`` is the
        handoff it is recorded against.
        """
        await self._publish(message, cid)
        if not isinstance(message, _INGRESS):
            raise TypeError(f"{type(message).__name__} is not an orchestrator ingress")
        pending: list[PrHandoffMessage] = [message]
        while pending:
            emitted = await self.orchestrator.handle(pending.pop(0))
            for event in emitted:
                event_cid = getattr(event, "correlation_id", cid)
                await self._publish(event, event_cid)
                if isinstance(event, ModelPrHandoffLedgerAppendCommand):
                    answer = await self.effect.handle(event)
                    await self._publish(answer, answer.correlation_id)
                    pending.append(answer)


async def drive(
    ledger_answers: Sequence[EnumWorkLedgerAppendStatus | None] | RealLedger,
    steps: Sequence[tuple[BaseModel, UUID]],
) -> HandoffRun:
    ledger = (
        ledger_answers
        if isinstance(ledger_answers, RealLedger)
        else ScriptedLedger(list(ledger_answers))
    )
    run = HandoffRun(ledger=ledger)
    for message, cid in steps:
        await run.send(message, cid=cid)
    return run


def new_cid() -> UUID:
    return uuid4()


__all__ = [
    "HEAD",
    "KEY",
    "LANE",
    "OTHER_HEAD",
    "PR",
    "REPO",
    "TICKET",
    "HandoffRun",
    "InMemoryLedgerFile",
    "RealLedger",
    "ScriptedLedger",
    "at",
    "drive",
    "lab_comment",
    "new_cid",
    "observation",
    "request",
    "stamp",
]
