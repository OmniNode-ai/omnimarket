# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J7 run (OMN-23420): answer each piece 7 golden reading with a send past a stop whose lab check posts on the head.

Canonical shape: ``handle(ModelJoinJ7RunCommand) -> ModelJoinJ7RunResult``. The J7 join test (tests/integration/test_join_j7.py)
judges the scenario by hand; nothing ran it, so the floor read J7 ``not_run``. This is the caller, built like the J6 caller
(``HandlerGateStoreJoinJ6Run``). Piece 7's golden chains are the stop drill's STATUS rows under the correlation id the floor
gives each row. For each such chain the journal does not show piece 8 answering, this run opens a stop on a test piece in an
in-memory gate store, checks that the store refuses a send of the stopped piece and admits a send of another piece, that the stop
gate admits the stop's cause lane and that its lab job posts the required check on the test head (the head then reads PASS),
that a send of another lane is refused and posts nothing, and writes piece 8's start and success (or a typed failure) to the
journal under that chain's id. A chain piece 8 already answered is not answered again, so a rerun posts nothing.

Piece 8's own error chain is the drill: a post of a superseded attempt posts nothing, takes the earlier attempt's counting
pass away and leaves the head reading SUPERSEDED. When it holds, it is written once per window as a drill chain ending in a
typed failure; when it does not hold, nothing is written and the result says so, so the floor never reads an error receipt
the check-run handler did not earn.

An unreadable ledger is never an empty answer: nothing is owed or written, and the result says so. A probe reads what is
owed and writes nothing. The twin is the in-memory gate store and check-run API; chains are written with that backend.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from omnimarket.handlers.emission_sink import ChainEmitter, ProtocolEmissionSink
from omnimarket.handlers.rules_stop_gate import (
    fold_stop_event,
    load_andon_stop_contract,
    stop_gate,
)
from omnimarket.models.dev_head_red import (
    dev_cause_dispatch_key,
    dev_cause_lane_name,
    dev_head_owner_key,
)
from omnimarket.models.floor_17a import ModelPieceEventRule
from omnimarket.models.lab_check_verdict import EnumLabCheckVerdict
from omnimarket.models.model_gate_store_join_j6 import (
    EnumJoinJ6Operation,
    EnumJoinJ6Outcome,
    ModelJoinJ6Request,
)
from omnimarket.models.model_lab_check_run_join_j7_run import (
    EnumJoinJ7RunOutcome,
    ModelJoinJ7RunCommand,
    ModelJoinJ7RunItem,
    ModelJoinJ7RunResult,
)
from omnimarket.models.stop_gate import (
    EnumStopEventKind,
    EnumStopGateOutcome,
    EnumStopGateReason,
    EnumStopMode,
    ModelStopEvent,
    ModelStopGateRequest,
    ModelStopGateResult,
    ModelStopPolicy,
    ModelStopRecord,
    ModelStopScope,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    piece_events,
)
from omnimarket.nodes.node_gate_store_effect.handlers.handler_gate_store_join_j6 import (
    HandlerGateStoreJoinJ6,
)
from omnimarket.nodes.node_lab_check_run_effect.adapters.inmemory_check_run_api import (
    InMemoryCheckRunApi,
)
from omnimarket.nodes.node_lab_check_run_effect.handlers.handler_lab_check_run_effect import (
    HandlerLabCheckRunEffect,
)
from omnimarket.nodes.node_lab_check_run_effect.models.model_lab_check_run import (
    EnumLabCheckRunConclusion,
    EnumLabCheckRunOperation,
    EnumLabCheckRunOutcome,
    ModelLabCheckRunRequest,
    ModelLabCheckRunResult,
)
from omnimarket.nodes.node_lab_check_run_effect.overlay_join_j7 import (
    ModelJoinJ7Overlay,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
)

_FUTURE_SLACK = timedelta(days=1)
_SUPERSEDED_CODE = "superseded_attempt_refused"
_HEAD_PASSING_CODE = "head_already_passing"


@dataclass(frozen=True)
class JoinJ7Rig:
    """One run's twin: the gate store join, the check-run API and the lab check handler over it."""

    join: HandlerGateStoreJoinJ6
    api: InMemoryCheckRunApi
    labcheck: HandlerLabCheckRunEffect
    release: Callable[[], None] = lambda: None


class HandlerLabCheckRunJoinJ7Run:
    def __init__(
        self,
        *,
        source: ProtocolLedgerRowSource,
        sink: ProtocolEmissionSink,
        upstream: ModelPieceEventRule,
        overlay: ModelJoinJ7Overlay,
        golden_rig: Callable[[], JoinJ7Rig],
        drill_rig: Callable[[], JoinJ7Rig],
        window_hours: int,
        to_piece: int,
        backend: str = "in_memory",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._source = source
        self._sink = sink
        self._rule = upstream
        self._overlay = overlay
        self._golden_rig = golden_rig
        self._drill_rig = drill_rig
        self._window = timedelta(hours=window_hours)
        self._to_piece = to_piece
        self._backend = backend
        self._clock = clock or (lambda: datetime.now(UTC))
        self._fsm, self._config = load_andon_stop_contract()

    def handle(self, request: ModelJoinJ7RunCommand) -> ModelJoinJ7RunResult:
        now = self._clock()
        rule = self._rule
        read = self._source.read(
            ModelLedgerQuery(
                since=now - timedelta(days=rule.lookback_days),
                until=now + _FUTURE_SLACK,
                row_type=rule.golden.row_type,
                lane=rule.golden.lane,
                correlation_id=request.correlation_id,
            )
        )
        if read.error is not None:
            return ModelJoinJ7RunResult(
                read_ok=False, reason=f"ledger unread: {read.error}"
            )
        since = now - self._window
        golden = [
            e
            for e in piece_events(list(read.rows), (rule,))
            if e.phase == "success" and not e.drill and e.emitted_at >= since
        ]
        journal = self._sink.read(since)
        answered_ids = {e.correlation_id for e in journal if e.piece == self._to_piece}
        drill_since = now - timedelta(hours=self._overlay.drill_window_hours)
        drill_owed = not any(
            e.piece == self._to_piece
            and e.drill
            and e.phase == "failure"
            and e.emitted_at >= drill_since
            for e in journal
        )
        seen: set[str] = set()
        owed: list[str] = []
        answered = 0
        for event in sorted(golden, key=lambda e: e.emitted_at, reverse=True):
            cid = event.correlation_id
            if cid in seen:
                continue
            seen.add(cid)
            if cid in answered_ids:
                answered += 1
            else:
                owed.append(cid)
        if request.probe:
            return ModelJoinJ7RunResult(
                read_ok=True, owed=len(owed), answered=answered, drill_owed=drill_owed
            )
        items = [self._answer(cid) for cid in owed[: self._overlay.max_chains_per_run]]
        if drill_owed:
            items.append(self._error_drill())
        return ModelJoinJ7RunResult(
            read_ok=True,
            owed=len(owed),
            answered=answered,
            drill_owed=drill_owed,
            items=tuple(items),
        )

    # -- the twin's two lanes of piece 8, named the way the dispatcher names a cause lane for a red
    def _lanes(self) -> tuple[str, str]:
        ov = self._overlay
        owner = dev_head_owner_key(
            ov.repository, ov.base_branch, ov.check_run.settings.check_name
        )
        cause = dev_cause_lane_name(
            ov.repository, dev_cause_dispatch_key(owner, ov.head_sha)
        )
        other = dev_cause_lane_name(
            ov.repository, dev_cause_dispatch_key(owner, ov.old_head_sha)
        )
        return cause, other

    def _join_request(
        self,
        cid: str,
        operation: EnumJoinJ6Operation,
        piece: str,
        step: str,
        reservation_id: str | None = None,
    ) -> ModelJoinJ6Request:
        return ModelJoinJ6Request(
            correlation_id=cid,
            operation=operation,
            piece=piece,
            step_key=f"{step}-{cid}",
            reservation_id=reservation_id,
            now=self._clock().timestamp(),
        )

    def _check_request(
        self,
        cid: str,
        operation: EnumLabCheckRunOperation,
        attempt: str,
        current: str,
        head: str,
        detail: str,
    ) -> ModelLabCheckRunRequest:
        ov = self._overlay
        now = self._clock()
        post = operation is EnumLabCheckRunOperation.POST
        return ModelLabCheckRunRequest(
            correlation_id=f"{cid}-{operation.value}-{attempt}",
            operation=operation,
            repository=ov.repository,
            head_sha=head,
            current_attempt_id=current,
            dispatched_at=now
            - timedelta(seconds=ov.check_run.settings.wait_bound_seconds // 4),
            now=now,
            attempt_id=attempt if post else None,
            conclusion=EnumLabCheckRunConclusion.SUCCESS if post else None,
            detail=detail,
        )

    def _read_head(
        self, rig: JoinJ7Rig, cid: str, current: str, head: str
    ) -> ModelLabCheckRunResult:
        return rig.labcheck.handle(
            self._check_request(
                cid, EnumLabCheckRunOperation.READ, current, current, head, ""
            )
        )

    def _open_stop(self, cid: str, cause_lane: str) -> ModelStopRecord:
        ov = self._overlay
        now = self._clock()
        scope = ModelStopScope(kind="piece", key=ov.stopped_piece)
        stopped_at = now - timedelta(minutes=ov.stop_age_minutes)
        opened = ModelStopRecord(
            id=ov.stop_id,
            cls=ov.stop_class,
            scope=scope,
            state="open",
            stopped_at=stopped_at,
        )
        return fold_stop_event(
            opened,
            ModelStopEvent.model_validate(
                {
                    "stop_id": ov.stop_id,
                    "kind": EnumStopEventKind.LEASE_GRANTED,
                    "role": self._config.writer_roles[
                        EnumStopEventKind.LEASE_GRANTED.value
                    ],
                    "at": stopped_at + timedelta(minutes=ov.lease_after_minutes),
                    "lane_id": cause_lane,
                }
            ),
            self._fsm,
            self._config,
        ).stop

    def _gate(self, record: ModelStopRecord, lane: str) -> ModelStopGateResult:
        ov = self._overlay
        return stop_gate(
            ModelStopGateRequest(
                scope=ModelStopScope(kind="piece", key=ov.stopped_piece),
                stops=(record,),
                policy=ModelStopPolicy(
                    modes={ov.stop_class: EnumStopMode.ENFORCE},
                ),
                now=self._clock(),
                lane_id=lane,
            ),
            self._fsm,
        )

    def _failed(
        self,
        chain: ChainEmitter,
        cid: str,
        outcome: EnumJoinJ7RunOutcome,
        code: str,
        detail: str,
    ) -> ModelJoinJ7RunItem:
        chain.failure(code, detail)
        return ModelJoinJ7RunItem(
            correlation_id=cid, outcome=outcome, failure_code=code, detail=detail
        )

    def _answer(self, cid: str) -> ModelJoinJ7RunItem:
        ov = self._overlay
        chain = ChainEmitter(
            self._sink,
            node=ov.emission_node,
            piece=self._to_piece,
            clock=self._clock,
            correlation_id=cid,
            backend=self._backend,
        )
        chain.start()
        rig = self._golden_rig()
        try:
            cause_lane, other_lane = self._lanes()
            if (
                self._read_head(rig, cid, ov.current_attempt_id, ov.head_sha).verdict
                is EnumLabCheckVerdict.PASS
            ):
                return self._failed(
                    chain,
                    cid,
                    EnumJoinJ7RunOutcome.HEAD_NOT_PASSING,
                    _HEAD_PASSING_CODE,
                    "the test head already reads PASS, so a PASS after the send would prove nothing",
                )
            stop = rig.join.handle(
                self._join_request(
                    cid, EnumJoinJ6Operation.STOP, ov.stopped_piece, "stop"
                )
            )
            if stop.outcome is not EnumJoinJ6Outcome.STOP_COMMITTED:
                return self._failed(
                    chain,
                    cid,
                    EnumJoinJ7RunOutcome.STOP_NOT_COMMITTED,
                    stop.failure_code or stop.outcome.value,
                    stop.detail,
                )
            record = self._open_stop(cid, cause_lane)
            refused = rig.join.handle(
                self._join_request(
                    cid,
                    EnumJoinJ6Operation.SEND,
                    ov.stopped_piece,
                    "send",
                    "res-a",
                )
            )
            if (
                refused.outcome is not EnumJoinJ6Outcome.SEND_REFUSED
                or refused.store_outcome != "refused_stop"
            ):
                return self._failed(
                    chain,
                    cid,
                    EnumJoinJ7RunOutcome.SEND_NOT_REFUSED,
                    EnumJoinJ7RunOutcome.SEND_NOT_REFUSED.value,
                    f"a send of the stopped piece read {refused.outcome.value} / {refused.store_outcome}",
                )
            other_piece = rig.join.handle(
                self._join_request(
                    cid,
                    EnumJoinJ6Operation.SEND,
                    ov.other_piece,
                    "send-other",
                    "res-b",
                )
            )
            if other_piece.outcome is not EnumJoinJ6Outcome.SEND_ADMITTED:
                return self._failed(
                    chain,
                    cid,
                    EnumJoinJ7RunOutcome.OTHER_PIECE_NOT_ADMITTED,
                    EnumJoinJ7RunOutcome.OTHER_PIECE_NOT_ADMITTED.value,
                    f"a send of another piece read {other_piece.outcome.value}",
                )
            cause = self._gate(record, cause_lane)
            if (
                cause.outcome is EnumStopGateOutcome.REFUSE
                or cause.reason is not EnumStopGateReason.SWARM_LANE
            ):
                return self._failed(
                    chain,
                    cid,
                    EnumJoinJ7RunOutcome.CAUSE_LANE_NOT_ADMITTED,
                    EnumJoinJ7RunOutcome.CAUSE_LANE_NOT_ADMITTED.value,
                    f"the stop gate read {cause.outcome.value} / {cause.reason.value} for the cause lane",
                )
            posted = rig.labcheck.handle(
                self._check_request(
                    cid,
                    EnumLabCheckRunOperation.POST,
                    ov.current_attempt_id,
                    ov.current_attempt_id,
                    ov.head_sha,
                    f"{cause_lane} on {ov.stopped_piece}",
                )
            )
            if (
                posted.outcome is not EnumLabCheckRunOutcome.POSTED
                or posted.posted is None
                or posted.posted.attempt_id != ov.current_attempt_id
            ):
                return self._failed(
                    chain,
                    cid,
                    EnumJoinJ7RunOutcome.CHECK_NOT_POSTED,
                    EnumJoinJ7RunOutcome.CHECK_NOT_POSTED.value,
                    f"the lab check read {posted.outcome.value}",
                )
            verdict = self._read_head(
                rig, cid, ov.current_attempt_id, ov.head_sha
            ).verdict
            if verdict is not EnumLabCheckVerdict.PASS:
                return self._failed(
                    chain,
                    cid,
                    EnumJoinJ7RunOutcome.HEAD_NOT_PASSING,
                    EnumJoinJ7RunOutcome.HEAD_NOT_PASSING.value,
                    f"the test head reads {verdict} after the cause lane's post",
                )
            calls = rig.api.calls
            refused_lane = self._gate(record, other_lane)
            runs_before = len(rig.api.runs)
            if (
                refused_lane.outcome is not EnumStopGateOutcome.REFUSE
                or rig.api.calls != calls
                or len(rig.api.runs) != runs_before
            ):
                return self._failed(
                    chain,
                    cid,
                    EnumJoinJ7RunOutcome.OTHER_LANE_NOT_REFUSED,
                    EnumJoinJ7RunOutcome.OTHER_LANE_NOT_REFUSED.value,
                    f"the stop gate read {refused_lane.outcome.value} / {refused_lane.reason.value} for another lane",
                )
            chain.success(
                {
                    "sends_refused": 1,
                    "sends_admitted": 1,
                    "checks_posted": 1,
                    "other_lane_refused": 1,
                }
            )
            return ModelJoinJ7RunItem(
                correlation_id=cid, outcome=EnumJoinJ7RunOutcome.LANDED
            )
        finally:
            rig.release()

    def _error_drill(self) -> ModelJoinJ7RunItem:
        """A late post of a superseded attempt: it posts nothing, and takes the earlier attempt's pass away."""
        ov = self._overlay
        cid = f"drill-j7-{uuid4().hex}"
        rig = self._drill_rig()
        try:
            cause_lane, _ = self._lanes()
            detail = f"{cause_lane} on {ov.stopped_piece}"
            earlier = rig.labcheck.handle(
                self._check_request(
                    cid,
                    EnumLabCheckRunOperation.POST,
                    ov.old_attempt_id,
                    ov.old_attempt_id,
                    ov.head_sha,
                    detail,
                )
            )
            runs = rig.api.runs
            late = rig.labcheck.handle(
                self._check_request(
                    cid,
                    EnumLabCheckRunOperation.POST,
                    ov.old_attempt_id,
                    ov.current_attempt_id,
                    ov.head_sha,
                    detail,
                )
            )
            verdict = self._read_head(
                rig, cid, ov.current_attempt_id, ov.head_sha
            ).verdict
            after = rig.api.runs
        finally:
            rig.release()
        held = (
            earlier.outcome is EnumLabCheckRunOutcome.POSTED
            and len(runs) == 1
            and late.outcome is EnumLabCheckRunOutcome.REFUSED_SUPERSEDED_ATTEMPT
            and late.posted is None
            and late.invalidated == (runs[0].check_run_id,)
            and len(after) == 1
            and after[0].conclusion == ov.check_run.settings.superseded_conclusion
            and verdict is EnumLabCheckVerdict.SUPERSEDED
        )
        if not held:
            return ModelJoinJ7RunItem(
                correlation_id=cid,
                outcome=EnumJoinJ7RunOutcome.DRILL_NOT_HELD,
                drill=True,
                failure_code=EnumJoinJ7RunOutcome.DRILL_NOT_HELD.value,
                detail=(
                    f"earlier post {earlier.outcome.value}; late post {late.outcome.value}"
                    f" (invalidated {late.invalidated}); the head reads {verdict}"
                ),
            )
        chain = ChainEmitter(
            self._sink,
            node=ov.emission_node,
            piece=self._to_piece,
            clock=self._clock,
            correlation_id=cid,
            backend=self._backend,
            drill=True,
        )
        chain.start()
        chain.failure(_SUPERSEDED_CODE, late.outcome.value)
        return ModelJoinJ7RunItem(
            correlation_id=cid,
            outcome=EnumJoinJ7RunOutcome.DRILL_RECORDED,
            drill=True,
            failure_code=_SUPERSEDED_CODE,
            detail=late.outcome.value,
        )


__all__ = ["HandlerLabCheckRunJoinJ7Run", "JoinJ7Rig"]
