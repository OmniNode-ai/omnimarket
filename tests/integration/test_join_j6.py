# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J6 (piece 6 to piece 7): a stop committed in the gate store is audited and fences its piece (OMN-22936).

Rebuild plan 4.1, J6. The gate store is the real in-memory twin, never a stub of its answers; the join handler is
``HandlerGateStoreJoinJ6`` over it with an in-memory audit sink and projection.

* golden: a stop committed in the store lands one durable audit row and one projected row for the same piece, and the
  next send of that piece is refused by the store, while a send admitted before the stop stays admitted;
* error: a stop whose commit gets no answer by its deadline refuses the send of that piece at the fence (the store is
  never asked), and a send of another piece is admitted;
* mutation: a sink that drops the audit row turns the golden and the error scenario red;
* stop drill (OMN-22941): a send above a stop on a test piece is refused, and the stop then clears on a go-and-see row and
  a WORKING reading, or is escalated once when it is not cleared by ``stale_after_hours``, and stays open.

Not covered here: running the join on the bus (the join answers by return value in these tests) and the Postgres backend
of the store, which is not built.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.handlers.rules_stop_gate import (
    fold_stop_event,
    load_andon_stop_contract,
    stop_gate,
)
from omnimarket.models.model_gate_store import (
    EnumGateStoreBackend,
    EnumGateStoreOperation,
    ModelGateStorePolicy,
    ModelGateStoreRequest,
    ModelGateStoreResult,
)
from omnimarket.models.model_gate_store_join_j6 import (
    EnumJoinJ6Operation,
    EnumJoinJ6Outcome,
    ModelJoinJ6AuditRow,
    ModelJoinJ6Policy,
    ModelJoinJ6Request,
    ModelJoinJ6Result,
)
from omnimarket.models.model_stop_escalation import (
    EnumStopEscalationOutcome,
    ModelStopEscalationRequest,
)
from omnimarket.models.stop_gate import (
    EnumFloorVerdict,
    EnumReadingOrigin,
    EnumReadingVerdict,
    EnumStopEventKind,
    EnumStopGateOutcome,
    EnumStopMode,
    ModelGoAndSeeRow,
    ModelStopEvent,
    ModelStopGateRequest,
    ModelStopPolicy,
    ModelStopReading,
    ModelStopRecord,
    ModelStopScope,
    ModelWorkingReading,
)
from omnimarket.nodes.node_gate_store_effect.handlers.adapters_join_j6 import (
    InMemoryJoinJ6AuditSink,
    InMemoryJoinJ6Projection,
)
from omnimarket.nodes.node_gate_store_effect.handlers.handler_gate_store_join_j6 import (
    HandlerGateStoreJoinJ6,
    load_join_j6_policy,
)
from omnimarket.nodes.node_gate_store_effect.handlers.store_in_memory import (
    InMemoryGateStore,
)
from omnimarket.nodes.node_stop_escalation_effect.handlers.adapters_stop_escalation import (
    InMemoryStopAlarm,
    InMemoryStopEscalationRows,
)
from omnimarket.nodes.node_stop_escalation_effect.handlers.handler_stop_escalation import (
    HandlerStopEscalation,
)
from omnimarket.protocols.protocol_gate_store import ProtocolGateStore

pytestmark = pytest.mark.unit

STOPPED = "piece_stopped"
OTHER = "piece_other"
SHORT_DEADLINE_S = 0.05
MUTATION = "drop_audit_row"


class SilentOnStopStore:
    """The real twin, except that a stop of a chosen piece gets no answer until released."""

    def __init__(self, inner: InMemoryGateStore, silent_pieces: frozenset[str]) -> None:
        self._inner = inner
        self._silent = silent_pieces
        self._release = threading.Event()
        self.reserves_asked: list[str] = []

    def transact(self, request: ModelGateStoreRequest) -> ModelGateStoreResult:
        if (
            request.operation is EnumGateStoreOperation.RECORD_SAFETY
            and request.piece in self._silent
        ):
            self._release.wait(timeout=5.0)
            raise TimeoutError("released after the join gave up")
        if request.operation is EnumGateStoreOperation.RESERVE:
            self.reserves_asked.append(request.piece or "")
        return self._inner.transact(request)

    def release(self) -> None:
        self._release.set()


class RaisingOnStopStore:
    """The real twin, except that a stop of a chosen piece raises."""

    def __init__(self, inner: InMemoryGateStore, piece: str) -> None:
        self._inner = inner
        self._piece = piece

    def transact(self, request: ModelGateStoreRequest) -> ModelGateStoreResult:
        if (
            request.operation is EnumGateStoreOperation.RECORD_SAFETY
            and request.piece == self._piece
        ):
            raise ConnectionError("the store is unreachable")
        return self._inner.transact(request)


class DroppingAuditSink(InMemoryJoinJ6AuditSink):
    """Mutation ``drop_audit_row``: the sink reports the append and keeps nothing."""

    def append_if_absent(self, row: ModelJoinJ6AuditRow) -> bool:
        return True


class DroppingProjection(InMemoryJoinJ6Projection):
    """A projection that never hears a row."""

    def project(self, row: ModelJoinJ6AuditRow) -> None:
        return None


@dataclass
class Rig:
    handler: HandlerGateStoreJoinJ6
    audit: InMemoryJoinJ6AuditSink
    projection: InMemoryJoinJ6Projection
    store: ProtocolGateStore
    policy: ModelJoinJ6Policy


def _store_policy() -> ModelGateStorePolicy:
    return ModelGateStorePolicy(
        backend=EnumGateStoreBackend.IN_MEMORY,
        limits={STOPPED: 3, OTHER: 3},
        reservation_ttl_seconds=60,
        order=(),
    )


def _build(
    *,
    audit: InMemoryJoinJ6AuditSink | None = None,
    projection: InMemoryJoinJ6Projection | None = None,
    wrap: str | None = None,
    deadline_s: float | None = None,
) -> Rig:
    policy = load_join_j6_policy()
    if deadline_s is not None:
        policy = policy.model_copy(update={"commit_deadline_seconds": deadline_s})
    twin = InMemoryGateStore(_store_policy())
    store: ProtocolGateStore = twin
    if wrap == "silent":
        store = SilentOnStopStore(twin, frozenset({STOPPED}))
    elif wrap == "raising":
        store = RaisingOnStopStore(twin, STOPPED)
    audit = audit if audit is not None else InMemoryJoinJ6AuditSink()
    projection = projection if projection is not None else InMemoryJoinJ6Projection()
    return Rig(
        HandlerGateStoreJoinJ6(store, audit, projection, policy),
        audit,
        projection,
        store,
        policy,
    )


def _stop(rig: Rig, piece: str, step: str, cid: str) -> ModelJoinJ6Result:
    return rig.handler.handle(
        ModelJoinJ6Request(
            correlation_id=cid,
            operation=EnumJoinJ6Operation.STOP,
            piece=piece,
            step_key=step,
            now=100.0,
        )
    )


def _send(rig: Rig, piece: str, step: str, rid: str) -> ModelJoinJ6Result:
    return rig.handler.handle(
        ModelJoinJ6Request(
            correlation_id=f"cid-{step}",
            operation=EnumJoinJ6Operation.SEND,
            piece=piece,
            step_key=step,
            reservation_id=rid,
            now=100.0,
        )
    )


@pytest.fixture
def silent_rig() -> Iterator[Rig]:
    rig = _build(wrap="silent", deadline_s=SHORT_DEADLINE_S)
    yield rig
    assert isinstance(rig.store, SilentOnStopStore)
    rig.store.release()


def scenario_golden(rig: Rig) -> None:
    """The golden join, as assertions; the mutation tests run it against a broken rig."""
    before = _send(rig, STOPPED, "send-1", "res-1")
    assert before.outcome is EnumJoinJ6Outcome.SEND_ADMITTED

    stop = _stop(rig, STOPPED, "stop-1", "cid-stop-1")
    assert stop.outcome is EnumJoinJ6Outcome.STOP_COMMITTED, stop
    assert stop.store_outcome == "ok"

    durable = rig.audit.rows_for(STOPPED)
    projected = rig.projection.rows_for(STOPPED)
    assert len(durable) == 1
    assert len(projected) == 1
    assert durable == projected
    assert durable[0].kind == rig.policy.audit_kind_committed
    assert durable[0].correlation_id == "cid-stop-1"
    assert durable[0].piece == STOPPED
    assert durable[0].epoch is not None

    after = _send(rig, STOPPED, "send-2", "res-2")
    assert after.outcome is EnumJoinJ6Outcome.SEND_REFUSED, after
    assert after.store_outcome == "refused_stop"


def scenario_error(rig: Rig) -> None:
    """The error join, as assertions; the mutation tests run it against a broken rig."""
    stop = _stop(rig, STOPPED, "stop-1", "cid-stop-1")
    assert stop.outcome is EnumJoinJ6Outcome.STOP_UNANSWERED, stop
    assert "no answer within" in stop.detail

    durable = rig.audit.rows_for(STOPPED)
    projected = rig.projection.rows_for(STOPPED)
    assert len(durable) == 1
    assert durable == projected
    assert durable[0].kind == rig.policy.audit_kind_unanswered
    assert durable[0].epoch is None

    refused = _send(rig, STOPPED, "send-1", "res-1")
    assert refused.outcome is EnumJoinJ6Outcome.SEND_REFUSED, refused
    assert refused.store_outcome is None
    assert STOPPED not in getattr(rig.store, "reserves_asked", [STOPPED])

    other = _send(rig, OTHER, "send-2", "res-2")
    assert other.outcome is EnumJoinJ6Outcome.SEND_ADMITTED, other
    assert other.store_outcome == "ok"


def test_golden_stop_lands_one_durable_and_one_projected_row_and_fences_the_piece() -> (
    None
):
    scenario_golden(_build())


def test_golden_replayed_stop_lands_no_second_row() -> None:
    rig = _build()
    first = _stop(rig, STOPPED, "stop-1", "cid-a")
    again = _stop(rig, STOPPED, "stop-1", "cid-b")
    assert first.outcome is EnumJoinJ6Outcome.STOP_COMMITTED
    assert again.outcome is EnumJoinJ6Outcome.STOP_COMMITTED
    assert again.store_outcome == "duplicate"
    assert len(rig.audit.rows_for(STOPPED)) == 1
    assert len(rig.projection.rows_for(STOPPED)) == 1


def test_golden_stop_of_one_piece_leaves_another_piece_sendable() -> None:
    rig = _build()
    _stop(rig, STOPPED, "stop-1", "cid-stop-1")
    assert (
        _send(rig, OTHER, "send-1", "res-1").outcome is EnumJoinJ6Outcome.SEND_ADMITTED
    )
    assert rig.audit.rows_for(OTHER) == []


def test_error_stop_with_no_answer_by_the_deadline_fails_the_fence_closed(
    silent_rig: Rig,
) -> None:
    scenario_error(silent_rig)


def test_error_store_fault_on_commit_is_no_answer_and_keeps_the_fence_closed() -> None:
    rig = _build(wrap="raising")
    stop = _stop(rig, STOPPED, "stop-1", "cid-stop-1")
    assert stop.outcome is EnumJoinJ6Outcome.STOP_UNANSWERED
    assert "ConnectionError" in stop.detail
    assert (
        _send(rig, STOPPED, "send-1", "res-1").outcome is EnumJoinJ6Outcome.SEND_REFUSED
    )
    assert (
        _send(rig, OTHER, "send-2", "res-2").outcome is EnumJoinJ6Outcome.SEND_ADMITTED
    )


def test_error_stop_the_store_refuses_is_audited_and_keeps_the_fence_closed() -> None:
    rig = _build()
    stop = rig.handler.handle(
        ModelJoinJ6Request(
            correlation_id="cid-x",
            operation=EnumJoinJ6Operation.STOP,
            piece="piece_unknown_to_the_store",
            step_key="stop-x",
        )
    )
    assert stop.outcome is EnumJoinJ6Outcome.STOP_REFUSED
    assert stop.store_outcome == "refused_invalid"
    rows = rig.audit.rows_for("piece_unknown_to_the_store")
    assert [r.kind for r in rows] == [rig.policy.audit_kind_refused]
    assert (
        _send(rig, "piece_unknown_to_the_store", "send-x", "res-x").outcome
        is EnumJoinJ6Outcome.SEND_REFUSED
    )


def test_error_projected_row_missing_is_a_typed_failure_with_the_fence_closed() -> None:
    rig = _build(projection=DroppingProjection())
    stop = _stop(rig, STOPPED, "stop-1", "cid-stop-1")
    assert stop.outcome is EnumJoinJ6Outcome.FAILED
    assert stop.failure_code == rig.policy.projected_row_missing_failure_code
    assert len(rig.audit.rows_for(STOPPED)) == 1
    assert (
        _send(rig, STOPPED, "send-1", "res-1").outcome is EnumJoinJ6Outcome.SEND_REFUSED
    )


def test_mutation_drop_audit_row_reddens_the_stop_commit_case() -> None:
    assert MUTATION == "drop_audit_row"
    scenario_golden(_build())  # control: the same scenario passes without the mutation
    with pytest.raises(AssertionError):
        scenario_golden(_build(audit=DroppingAuditSink()))


def test_mutation_drop_audit_row_reddens_the_unanswered_commit_case() -> None:
    assert MUTATION == "drop_audit_row"
    clean = _build(wrap="silent", deadline_s=SHORT_DEADLINE_S)
    broken = _build(
        audit=DroppingAuditSink(), wrap="silent", deadline_s=SHORT_DEADLINE_S
    )
    try:
        scenario_error(clean)  # control: the same scenario passes without the mutation
        with pytest.raises(AssertionError):
            scenario_error(broken)
    finally:
        for rig in (clean, broken):
            assert isinstance(rig.store, SilentOnStopStore)
            rig.store.release()


def test_mutation_drop_audit_row_is_a_typed_failure_not_a_silent_success() -> None:
    rig = _build(audit=DroppingAuditSink())
    stop = _stop(rig, STOPPED, "stop-1", "cid-stop-1")
    assert stop.outcome is EnumJoinJ6Outcome.FAILED
    assert stop.failure_code == rig.policy.audit_row_missing_failure_code


def test_policy_reads_the_contract_section() -> None:
    policy = load_join_j6_policy()
    assert (policy.join_id, policy.from_piece, policy.to_piece) == ("J6", 6, 7)
    assert policy.commit_deadline_seconds > 0
    with pytest.raises(ValueError, match="join_j6"):
        load_join_j6_policy({"name": "x"})


# ---- the stop drill (OMN-22941): a send above a stop on a test piece is refused; the stop then clears or escalates ----

DRILL_T0 = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
DRILL_FSM, DRILL_CONFIG = load_andon_stop_contract()
DRILL_CLASS = "engine-policy-breach"
DRILL_SCOPE = ModelStopScope(kind="piece", key=STOPPED)
DRILL_POLICY = ModelStopPolicy(modes={DRILL_CLASS: EnumStopMode.ENFORCE})


def _drill_at(minutes: int) -> datetime:
    return DRILL_T0 + timedelta(minutes=minutes)


def _drill_event(
    kind: EnumStopEventKind, minutes: int, role: str, **fields: object
) -> ModelStopEvent:
    return ModelStopEvent(
        stop_id="drill-stop",
        kind=kind,
        role=role,
        at=_drill_at(minutes),
        **fields,  # type: ignore[arg-type]
    )


def _drill_fixed_reading(working_at: int | None) -> ModelStopReading:
    return ModelStopReading(
        verdict=EnumReadingVerdict.PASS,
        proof_eligible=True,
        origin=EnumReadingOrigin.EXECUTED,
        covers_scope=True,
        working=None
        if working_at is None
        else ModelWorkingReading(
            verdict=EnumFloorVerdict.WORKING, read_at=_drill_at(working_at)
        ),
    )


def _drill_stop_admits(record: ModelStopRecord) -> EnumStopGateOutcome:
    return stop_gate(
        ModelStopGateRequest(
            scope=DRILL_SCOPE,
            stops=(record,),
            policy=DRILL_POLICY,
            now=_drill_at(0),
            lane_id="some-other-lane",
        ),
        DRILL_FSM,
    ).outcome


def _drill_open_stop_on_the_test_piece(rig: Rig) -> ModelStopRecord:
    """The test piece is stopped in the gate store (J6) and in the stop queue's record; a send is then refused."""
    assert (
        _send(rig, STOPPED, "send-before", "res-before").outcome
        is EnumJoinJ6Outcome.SEND_ADMITTED
    )
    committed = _stop(rig, STOPPED, "stop-drill", "cid-drill")
    assert committed.outcome is EnumJoinJ6Outcome.STOP_COMMITTED
    record = ModelStopRecord(
        id="drill-stop",
        cls=DRILL_CLASS,
        scope=DRILL_SCOPE,
        state="open",
        stopped_at=DRILL_T0,
    )
    refused = _send(rig, STOPPED, "send-above-stop", "res-above-stop")
    assert refused.outcome is EnumJoinJ6Outcome.SEND_REFUSED, refused
    assert _drill_stop_admits(record) is EnumStopGateOutcome.REFUSE
    assert (
        _send(rig, OTHER, "send-other", "res-other").outcome
        is EnumJoinJ6Outcome.SEND_ADMITTED
    )
    return record


def test_stop_drill_the_stop_clears_on_a_go_and_see_row_and_a_working_reading() -> None:
    rig = _build()
    record = _drill_open_stop_on_the_test_piece(rig)
    record = fold_stop_event(
        record,
        _drill_event(
            EnumStopEventKind.LEASE_GRANTED, 1, "swarm_lane", lane_id="lane-drill"
        ),
        DRILL_FSM,
        DRILL_CONFIG,
    ).stop
    record = fold_stop_event(
        record,
        _drill_event(EnumStopEventKind.COUNTERMEASURE_MERGED, 2, "swarm_lane"),
        DRILL_FSM,
        DRILL_CONFIG,
    ).stop
    cleared = None
    for minute in (3, 4):
        result = fold_stop_event(
            record,
            _drill_event(
                EnumStopEventKind.READBACK,
                minute,
                "readback",
                reading=_drill_fixed_reading(working_at=minute),
                go_and_see=ModelGoAndSeeRow(
                    row_id="gas-drill",
                    lane_id="lane-drill",
                    seen_at=_drill_at(3),
                    observed="went to the test piece and saw its send path work",
                ),
            ),
            DRILL_FSM,
            DRILL_CONFIG,
        )
        record, cleared = result.stop, result
    assert record.state == "cleared"
    assert cleared is not None
    assert cleared.release is not None
    assert cleared.release.go_and_see_row_id == "gas-drill"
    assert _drill_stop_admits(record) is EnumStopGateOutcome.ADMIT
    assert (
        len(rig.audit.rows_for(STOPPED)) == 1
    )  # the audit of the stop is not erased by its clearing


def test_stop_drill_fixed_readings_alone_leave_the_stop_open_and_it_escalates_once() -> (
    None
):
    rig = _build()
    record = _drill_open_stop_on_the_test_piece(rig)
    record = fold_stop_event(
        record,
        _drill_event(EnumStopEventKind.COUNTERMEASURE_MERGED, 1, "swarm_lane"),
        DRILL_FSM,
        DRILL_CONFIG,
    ).stop
    for minute in (2, 3, 4):  # control: before OMN-22941 two of these cleared the stop
        record = fold_stop_event(
            record,
            _drill_event(
                EnumStopEventKind.READBACK,
                minute,
                "readback",
                reading=_drill_fixed_reading(working_at=None),
            ),
            DRILL_FSM,
            DRILL_CONFIG,
        ).stop
    assert record.state == "fixed"
    assert record.fixed_readings_seen == 3
    assert _drill_stop_admits(record) is EnumStopGateOutcome.REFUSE
    assert (
        _send(rig, STOPPED, "send-still-above", "res-still").outcome
        is EnumJoinJ6Outcome.SEND_REFUSED
    )

    rows = InMemoryStopEscalationRows()
    stale_at = DRILL_T0 + timedelta(hours=DRILL_CONFIG.stale_after_hours)
    alarm = InMemoryStopAlarm("drill-alarm", stale_at)
    escalation = HandlerStopEscalation(rows, alarm)
    early = escalation.handle(
        ModelStopEscalationRequest(stop=record, now=stale_at - timedelta(seconds=1))
    )
    assert early.outcome is EnumStopEscalationOutcome.NOT_STALE
    first = escalation.handle(ModelStopEscalationRequest(stop=record, now=stale_at))
    second = escalation.handle(
        ModelStopEscalationRequest(stop=record, now=stale_at + timedelta(hours=1))
    )
    assert first.outcome is EnumStopEscalationOutcome.ESCALATED
    assert first.receipt is not None
    assert second.outcome is EnumStopEscalationOutcome.ALREADY_ESCALATED
    assert len(rows.all_rows()) == 1
    assert alarm.deliveries == 1
    assert first.stop.state == "fixed"  # escalating leaves the stop open
    assert _drill_stop_admits(first.stop) is EnumStopGateOutcome.REFUSE
    assert (
        _send(rig, STOPPED, "send-after-escalation", "res-after").outcome
        is EnumJoinJ6Outcome.SEND_REFUSED
    )
