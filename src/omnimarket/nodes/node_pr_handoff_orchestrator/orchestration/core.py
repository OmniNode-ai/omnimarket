# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One orchestrator leg: stored row plus one message gives the next row plus emissions (OMN-20636).

The transitions are the contract's ``state_machine`` (held equal to
:data:`TRANSITIONS` by a unit test). The leg is deterministic: time comes only
from the message (``requested_at``, ``observed_at``, ``answered_at``), the
ledger request id is derived from the request's correlation id, and the
decision compute it calls is pure.

Drops (no transition, nothing emitted): a redelivered request, an observation
older than the one the row holds, an observation with no waiting request (it is
still folded), and a ledger answer for another request id or attempt. A request
for a PR whose handoff is being appended is answered with an invalid_request
failure and leaves the row alone: the rows in flight belong to the earlier
request.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol
from uuid import NAMESPACE_OID, UUID, uuid5

from omnimarket.events.pr_state import ModelPrStateEmitRequest
from omnimarket.models.pr_handoff import (
    EnumPrHandoffErrorCode,
    EnumPrHandoffLedgerStatus,
    EnumPrHandoffState,
    EnumPrHandoffVerdict,
    ModelPrHandoffAccepted,
    ModelPrHandoffDecision,
    ModelPrHandoffDecisionRequest,
    ModelPrHandoffFailed,
    ModelPrHandoffHandedOff,
    ModelPrHandoffLedgerAppendCommand,
    ModelPrHandoffLedgerAppended,
    ModelPrHandoffRequested,
)
from omnimarket.nodes.node_pr_handoff_orchestrator.models.model_pr_handoff_observation_ingress import (
    ModelPrHandoffObservationIngress,
)
from omnimarket.nodes.node_pr_handoff_orchestrator.models.model_pr_handoff_workflow_row import (
    ANSWERED_MEMORY,
    ModelPrHandoffEpisode,
    ModelPrHandoffWorkflowRow,
)

PR_HANDOFF_NAMESPACE = uuid5(NAMESPACE_OID, "onex.omnimarket.pr-handoff")
MAX_APPEND_ATTEMPTS = 3

S = EnumPrHandoffState
E = EnumPrHandoffErrorCode

# (from_state, trigger) -> to_state, exactly the contract's state_machine.
TRANSITIONS: Mapping[tuple[str, str], str] = {
    ("REQUESTED", "accepted"): "WAITING",
    ("REQUESTED", "rejected_invalid_request"): "REFUSED",
    ("WAITING", "evaluated_wait"): "WAITING",
    ("WAITING", "evaluated_ready"): "APPENDING",
    ("WAITING", "evaluated_stale_head"): "REFUSED",
    ("WAITING", "evaluated_missing_ticket"): "REFUSED",
    ("WAITING", "evaluated_not_owned"): "REFUSED",
    ("WAITING", "evaluated_missing_lab_proof"): "REFUSED",
    ("WAITING", "evaluated_held"): "REFUSED",
    ("WAITING", "evaluated_withheld"): "REFUSED",
    ("WAITING", "evaluated_pr_not_open"): "REFUSED",
    ("WAITING", "superseded"): "REFUSED",
    ("WAITING", "completion_bound_expired"): "TIMED_OUT",
    ("APPENDING", "append_accepted"): "HANDED_OFF",
    ("APPENDING", "append_pending"): "APPENDING",
    ("APPENDING", "append_refused"): "REFUSED",
    ("APPENDING", "append_attempts_exhausted"): "TIMED_OUT",
}

PrHandoffEmission = (
    ModelPrHandoffAccepted
    | ModelPrHandoffHandedOff
    | ModelPrHandoffFailed
    | ModelPrHandoffLedgerAppendCommand
)
PrHandoffMessage = (
    ModelPrHandoffRequested
    | ModelPrHandoffObservationIngress
    | ModelPrHandoffLedgerAppended
)


class ProtocolPrHandoffDecider(Protocol):
    """node_pr_handoff_decision_compute's handler, called in process."""

    def handle(
        self, request: ModelPrHandoffDecisionRequest
    ) -> ModelPrHandoffDecision: ...


class ProtocolPrHandoffHoldReader(Protocol):
    """Live ledger HOLD ids that name the PR (repo#n) or the requesting lane."""

    def holds_for(self, handoff_key: str, lane: str) -> tuple[str, ...]: ...


class NoLedgerHolds:
    """The hold reader when none is wired: the PR's own hold markers still apply.

    The requesting lane's inbox gate (an open HOLD addressed to it) runs before
    it publishes, and the landing controller re-reads ledger holds before any
    merge, so an unwired reader never lets a held PR merge.
    """

    def holds_for(self, handoff_key: str, lane: str) -> tuple[str, ...]:
        del handoff_key, lane
        return ()


@dataclass(frozen=True)
class PrHandoffPorts:
    decider: ProtocolPrHandoffDecider
    holds: ProtocolPrHandoffHoldReader = field(default_factory=NoLedgerHolds)


@dataclass
class PrHandoffStepResult:
    """The row to write (None: nothing to write) and what to publish, in order."""

    row: ModelPrHandoffWorkflowRow | None
    emitted: list[PrHandoffEmission] = field(default_factory=list)
    dropped_reason: str | None = None


def _req[T](value: T | None, what: str) -> T:
    """``value``, or a RuntimeError naming what the row was missing (a broken invariant)."""
    if value is None:
        msg = f"the handoff row has no {what}"
        raise RuntimeError(msg)
    return value


def check_transition(state: EnumPrHandoffState, trigger: str) -> EnumPrHandoffState:
    """The contract's target state; any other edge is a programming error."""
    target = TRANSITIONS.get((state.value, trigger))
    if target is None:
        msg = f"no transition from {state.value} on {trigger}"
        raise ValueError(msg)
    return EnumPrHandoffState(target)


def ledger_request_id_for(correlation_id: UUID) -> UUID:
    """One ledger request id per handoff, shared by every attempt (the ledger host dedups on it)."""
    return uuid5(PR_HANDOFF_NAMESPACE, f"{correlation_id}|ledger-append")


def _observed_at(observation: ModelPrStateEmitRequest) -> datetime:
    return datetime.fromisoformat(observation.observed_at.replace("Z", "+00:00"))


def _failed(
    request: ModelPrHandoffRequested,
    code: EnumPrHandoffErrorCode,
    state: EnumPrHandoffState,
    detail: str,
    at: datetime,
    *,
    live_head_sha: str | None = None,
    ledger_request_id: UUID | None = None,
) -> ModelPrHandoffFailed:
    return ModelPrHandoffFailed(
        correlation_id=request.correlation_id,
        handoff_key=request.handoff_key,
        repo=request.repo,
        pr_number=request.pr_number,
        lane=request.lane,
        error_code=code,
        terminal_state=state,
        detail=detail[:2000],
        live_head_sha=live_head_sha,
        ledger_request_id=ledger_request_id,
        failed_at=at,
    )


class _Leg:
    def __init__(self, row: ModelPrHandoffWorkflowRow, ports: PrHandoffPorts) -> None:
        self.row = row
        self.ports = ports
        self.emitted: list[PrHandoffEmission] = []

    @property
    def episode(self) -> ModelPrHandoffEpisode | None:
        return self.row.episode

    def emit(self, event: PrHandoffEmission) -> None:
        """Queue ``event``; a terminal also records its request as answered."""
        self.emitted.append(event)
        if isinstance(event, ModelPrHandoffFailed | ModelPrHandoffHandedOff):
            self.mark_answered(event.correlation_id)

    def mark_answered(self, correlation_id: UUID) -> None:
        answered = (*self.row.answered, str(correlation_id))[-ANSWERED_MEMORY:]
        self.row = self.row.model_copy(update={"answered": answered})

    def expire(self, at: datetime, why: str) -> None:
        episode = _req(self.episode, "an episode")
        self._move("completion_bound_expired")
        self.emit(
            _failed(
                episode.request,
                E.TIMED_OUT,
                S.TIMED_OUT,
                f"{why} at the {episode.request.wait_budget_s}s budget (deadline {episode.deadline_at})",
                at,
            )
        )

    def past_deadline(self, at: datetime) -> bool:
        episode = _req(self.episode, "an episode")
        return at >= _req(episode.deadline_at, "the wait deadline")

    def _set_episode(self, episode: ModelPrHandoffEpisode) -> None:
        self.row = self.row.model_copy(update={"episode": episode})

    def _move(self, trigger: str, **update: object) -> ModelPrHandoffEpisode:
        episode = _req(self.episode, "an episode")
        target = check_transition(episode.state, trigger)
        moved = episode.model_copy(update={"state": target, **update})
        self._set_episode(moved)
        return moved

    def supersede(self, at: datetime) -> None:
        episode = _req(self.episode, "an episode")
        if self.past_deadline(at):
            self.expire(at, "a newer request arrived after this one's wait had run out")
            return
        self._move("superseded")
        self.emit(
            _failed(
                episode.request,
                E.SUPERSEDED,
                S.REFUSED,
                "a newer handoff request for this PR replaced this one while it waited",
                at,
            )
        )

    def abandon(self, at: datetime) -> None:
        episode = _req(self.episode, "an episode")
        self._move("append_attempts_exhausted")
        self.emit(
            _failed(
                episode.request,
                E.APPEND_UNCONFIRMED,
                S.TIMED_OUT,
                "the runtime abandoned the append in flight; read the ledger for "
                f"req={episode.ledger_request_id} before handing the PR off again",
                at,
                ledger_request_id=episode.ledger_request_id,
            )
        )

    def start(self, request: ModelPrHandoffRequested, reason: str | None) -> None:
        self._set_episode(ModelPrHandoffEpisode(request=request, state=S.REQUESTED))
        at = request.requested_at
        if reason is not None:
            self._move("rejected_invalid_request")
            self.emit(_failed(request, E.INVALID_REQUEST, S.REFUSED, reason, at))
            return
        deadline = at + timedelta(seconds=request.wait_budget_s)
        self._move("accepted", deadline_at=deadline)
        self.emit(
            ModelPrHandoffAccepted(
                correlation_id=request.correlation_id,
                handoff_key=request.handoff_key,
                repo=request.repo,
                pr_number=request.pr_number,
                lane=request.lane,
                expected_head_sha=request.expected_head_sha,
                accepted_at=at,
                deadline_at=deadline,
            )
        )

    def evaluate(self, now: datetime) -> None:
        episode = _req(self.episode, "an episode")
        if episode.state is not S.WAITING:
            msg = f"evaluate needs WAITING, the row is {episode.state.value}"
            raise RuntimeError(msg)
        request = episode.request
        if self.past_deadline(now):
            last = episode.last_decision
            reason = (
                f"still waiting ({last.wait_reason.value}: {last.detail})"
                if last is not None and last.wait_reason is not None
                else "never decided"
            )
            self.expire(now, reason)
            return
        decision = self.ports.decider.handle(
            ModelPrHandoffDecisionRequest(
                request=request,
                observation=self.row.observation,
                ledger_holds=self.ports.holds.holds_for(
                    request.handoff_key, request.lane
                ),
                now=now,
            )
        )
        if decision.verdict is EnumPrHandoffVerdict.READY:
            rows = _req(decision.rows, "the ready decision's rows")
            ledger_id = ledger_request_id_for(request.correlation_id)
            self._move(
                "evaluated_ready",
                last_decision=decision,
                ledger_request_id=ledger_id,
                rows=rows,
                append_attempts=1,
            )
            self._append(now)
            return
        if decision.verdict is EnumPrHandoffVerdict.REFUSE:
            code = _req(decision.error_code, "the refusal's error code")
            self._move(f"evaluated_{code.value}", last_decision=decision)
            self.emit(
                _failed(
                    request,
                    code,
                    S.REFUSED,
                    decision.detail,
                    now,
                    live_head_sha=decision.live_head_sha,
                )
            )
            return
        self._move("evaluated_wait", last_decision=decision)

    def _append(self, now: datetime) -> None:
        episode = _req(self.episode, "an episode")
        request = episode.request
        self.emit(
            ModelPrHandoffLedgerAppendCommand(
                correlation_id=request.correlation_id,
                handoff_key=request.handoff_key,
                ledger_request_id=_req(
                    episode.ledger_request_id, "the ledger request id"
                ),
                attempt=episode.append_attempts,
                rows=_req(episode.rows, "the handoff rows"),
                requested_by_lane=request.lane,
                requesting_host=request.requesting_host,
                requested_at=now,
            )
        )

    def answer(self, answer: ModelPrHandoffLedgerAppended) -> None:
        episode = _req(self.episode, "an episode")
        request = episode.request
        decision = episode.last_decision
        at = answer.answered_at
        status = answer.status
        if status in (
            EnumPrHandoffLedgerStatus.ACCEPTED,
            EnumPrHandoffLedgerStatus.DUPLICATE,
        ):
            self._move("append_accepted")
            ready = _req(decision, "the ready decision")
            self.emit(
                ModelPrHandoffHandedOff(
                    correlation_id=request.correlation_id,
                    handoff_key=request.handoff_key,
                    repo=request.repo,
                    pr_number=request.pr_number,
                    head_sha=_req(ready.live_head_sha, "the live head"),
                    lane=request.lane,
                    to_lane=request.to_lane,
                    ticket=_req(ready.ticket, "the ticket"),
                    msg_id=_req(ready.msg_id, "the MSG id"),
                    ledger_request_id=_req(
                        episode.ledger_request_id, "the ledger request id"
                    ),
                    ledger_lines=answer.ledger_lines,
                    handed_off_at=at,
                )
            )
            return
        if status is EnumPrHandoffLedgerStatus.PENDING:
            if episode.append_attempts < MAX_APPEND_ATTEMPTS:
                self._move(
                    "append_pending", append_attempts=episode.append_attempts + 1
                )
                self._append(at)
                return
            self._move("append_attempts_exhausted")
            self.emit(
                _failed(
                    request,
                    E.APPEND_UNCONFIRMED,
                    S.TIMED_OUT,
                    f"{MAX_APPEND_ATTEMPTS} append attempts ended with no receipt; read the ledger "
                    f"for req={episode.ledger_request_id} before handing the PR off again",
                    at,
                    ledger_request_id=episode.ledger_request_id,
                )
            )
            return
        self._move("append_refused")
        self.emit(
            _failed(
                request,
                E.LEDGER_REFUSED,
                S.REFUSED,
                f"the ledger host answered {status.value}: {answer.message}",
                at,
                ledger_request_id=episode.ledger_request_id,
            )
        )


def _new_row(key: str) -> ModelPrHandoffWorkflowRow:
    return ModelPrHandoffWorkflowRow(handoff_key=key)


_SUCCESS = (EnumPrHandoffLedgerStatus.ACCEPTED, EnumPrHandoffLedgerStatus.DUPLICATE)


def message_key(message: PrHandoffMessage) -> str:
    """The identity of a consumed message, for redelivery."""
    if isinstance(message, ModelPrHandoffRequested):
        return f"request:{message.correlation_id}"
    if isinstance(message, ModelPrHandoffObservationIngress):
        return f"observed:{message.observed_at}:{message.digest or message.head_sha}"
    return (
        f"answer:{message.ledger_request_id}:{message.attempt}:{message.status.value}"
    )


def _on_request(
    leg: _Leg, message: ModelPrHandoffRequested, invalid: str | None
) -> str | None:
    """Apply a request; return a drop reason when it changes nothing."""
    episode = leg.episode
    cid = str(message.correlation_id)
    if cid in leg.row.answered:
        return "a request that already has its terminal"
    if episode is not None and episode.request.correlation_id == message.correlation_id:
        return "a redelivered request already in flight"
    at = message.requested_at
    if episode is not None and not episode.state.is_terminal:
        if episode.request.requested_at > at:
            # Older than the request in flight: it lost the race, and is answered so.
            leg.emit(
                _failed(
                    message,
                    E.SUPERSEDED,
                    S.REFUSED,
                    f"request {episode.request.correlation_id} for this PR is newer",
                    at,
                )
            )
            return None
        if episode.state is S.APPENDING and episode.append_abandoned:
            leg.abandon(at)
        elif episode.state is S.APPENDING:
            leg.emit(
                _failed(
                    message,
                    E.INVALID_REQUEST,
                    S.REFUSED,
                    f"a handoff of {message.handoff_key} by request "
                    f"{episode.request.correlation_id} is being appended; read its "
                    "terminal before requesting again",
                    at,
                )
            )
            return None
        elif episode.state is S.WAITING:
            leg.supersede(at)
    leg.start(message, invalid)
    if leg.episode is not None and leg.episode.state is S.WAITING:
        leg.evaluate(at)
    return None


def _on_answer(leg: _Leg, message: ModelPrHandoffLedgerAppended) -> str | None:
    episode = leg.episode
    if (
        episode is None
        or episode.state is not S.APPENDING
        or episode.ledger_request_id != message.ledger_request_id
        or message.attempt > episode.append_attempts
    ):
        return "an answer for no append in flight"
    if message.attempt < episode.append_attempts and message.status not in _SUCCESS:
        # An earlier attempt's pending or refusal is obsolete; its success is not:
        # the rows are on the ledger under this request id.
        return "an obsolete answer for an earlier attempt"
    leg.answer(message)
    return None


def run_leg(
    row: ModelPrHandoffWorkflowRow | None,
    message: PrHandoffMessage,
    *,
    ports: PrHandoffPorts,
    invalid_reason: Callable[[ModelPrHandoffRequested], str | None],
) -> PrHandoffStepResult:
    """Apply one message to the PR's row.

    A redelivery of the message the last leg consumed re-emits what that leg
    emitted and writes nothing, so a publish lost after the row was written is
    recovered by the bus's redelivery (until the rows have a durable outbox,
    OMN-20638).
    """
    start = row if row is not None else _new_row(message.handoff_key)
    key = message_key(message)
    if start.last_message_key == key:
        return PrHandoffStepResult(
            row=None, emitted=list(start.last_emitted), dropped_reason=None
        )
    leg = _Leg(start, ports)
    dropped: str | None
    if isinstance(message, ModelPrHandoffRequested):
        dropped = _on_request(leg, message, invalid_reason(message))
    elif isinstance(message, ModelPrHandoffObservationIngress):
        current = start.observation
        if current is not None and _observed_at(current) > _observed_at(message):
            dropped = "an older observation"
        else:
            dropped = None
            leg.row = leg.row.model_copy(update={"observation": message.observation()})
            episode = leg.episode
            if episode is not None and episode.state is S.WAITING:
                # A decision is never made earlier than the request it answers.
                leg.evaluate(max(_observed_at(message), episode.request.requested_at))
    else:
        dropped = _on_answer(leg, message)
    if dropped is not None:
        return PrHandoffStepResult(row=None, dropped_reason=dropped)
    leg.row = leg.row.model_copy(
        update={"last_message_key": key, "last_emitted": tuple(leg.emitted)}
    )
    return PrHandoffStepResult(row=leg.row, emitted=leg.emitted)


__all__: list[str] = [
    "MAX_APPEND_ATTEMPTS",
    "PR_HANDOFF_NAMESPACE",
    "TRANSITIONS",
    "NoLedgerHolds",
    "PrHandoffEmission",
    "PrHandoffMessage",
    "PrHandoffPorts",
    "PrHandoffStepResult",
    "ProtocolPrHandoffDecider",
    "ProtocolPrHandoffHoldReader",
    "check_transition",
    "ledger_request_id_for",
    "message_key",
    "run_leg",
]
