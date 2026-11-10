# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J7 (piece 7 to piece 8): a send admitted past a stop on piece 8 posts its lab check on the head (OMN-22936).

Rebuild plan 4.1, J7, built from existing handlers only: the real in-memory gate store behind ``HandlerGateStoreJoinJ6``
commits the stop on piece 8, ``stop_gate`` (over a stop record the andon machine itself folded to a granted lease) is
the lane-aware admission, and the lab-bound job posts through the real ``HandlerLabCheckRunEffect`` over the in-memory
check-run API. Nothing here stubs an answer of the store, the gate or the check-run API.

* golden: with a stop open on piece 8, the send of the stop's cause lane is admitted and its job posts the required check
  on the test head carrying the attempt identity (dispatch, order and overlay); the head reads PASS for the current
  attempt; a send of another lane of piece 8 is refused and posts nothing;
* error: a post from a superseded attempt posts nothing and the earlier attempt's counting run is invalidated; the test
  refuses a head that already holds a PASS, and the handler's own answer for a repeated or competing PASS is pinned;
* mutation: a stop gate that ignores the stop turns the golden and the error scenario red, and a check-run API that
  accepts the invalidation without applying it turns the error scenario red.

Not covered here: the bus (every join answers by return value in these tests) and the GitHub backend of the check run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
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
from omnimarket.models.lab_check_verdict import EnumLabCheckVerdict
from omnimarket.models.model_gate_store import (
    EnumGateStoreBackend,
    ModelGateStorePolicy,
)
from omnimarket.models.model_gate_store_join_j6 import (
    EnumJoinJ6Operation,
    EnumJoinJ6Outcome,
    ModelJoinJ6Request,
    ModelJoinJ6Result,
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
    ModelCheckRunRecord,
    ModelLabCheckRunRequest,
    ModelLabCheckRunResult,
)
from tests.nodes.node_lab_check_run_effect.test_golden_chain_lab_check_run_effect import (
    CURRENT,
    HEAD,
    NOW,
    OLD,
    OLD_HEAD,
    REPO,
    overlay,
)

pytestmark = pytest.mark.unit

OV = overlay()
FSM, CONFIG = load_andon_stop_contract()
#: Piece 8 as the check-run overlay's own emission identity names it, and the gate store's name for it.
PIECE_8 = f"piece_{OV.emission.piece}"
#: The other piece of the store: the one before piece 8, which no stop touches.
PIECE_7 = f"piece_{OV.emission.piece - 1}"
STOP_CLASS = CONFIG.stop_classes[0].id
STOP_SCOPE = ModelStopScope(kind="piece", key=PIECE_8)
STOP_POLICY = ModelStopPolicy(modes={STOP_CLASS: EnumStopMode.ENFORCE})
STOP_ID = "j7-stop"
STOPPED_AT = NOW - timedelta(minutes=CONFIG.lease_minutes // 3)
LEASE_AT = STOPPED_AT + timedelta(minutes=1)
#: The cause lane the stop's lease names, minted the way the dispatcher mints one for a red.
OWNER_KEY = dev_head_owner_key(REPO, "dev", OV.settings.check_name)
CAUSE_LANE = dev_cause_lane_name(REPO, dev_cause_dispatch_key(OWNER_KEY, HEAD))
#: A lane of piece 8 that is not the stop's cause lane.
OTHER_LANE = dev_cause_lane_name(REPO, dev_cause_dispatch_key(OWNER_KEY, OLD_HEAD))
MUTATIONS = ("ignore_stop", "deaf_invalidation")


# ---- the attempt identity: dispatch, order and overlay, as the check-run fixtures declare it ----


def identity_parts(attempt_id: str) -> tuple[str, str, str]:
    dispatch, order, overlay_id = attempt_id.split(":")
    return dispatch, order, overlay_id


# ---- the injected breaks ----


class DeafInvalidationApi(InMemoryCheckRunApi):
    """Mutation ``deaf_invalidation``: the API accepts an update that would invalidate a run and changes nothing."""

    def update_run(self, **kwargs: Any) -> ModelCheckRunRecord:
        current = next(r for r in self.runs if r.check_run_id == kwargs["check_run_id"])
        if kwargs["conclusion"] == OV.settings.superseded_conclusion:
            return current
        return super().update_run(**kwargs)


@dataclass(frozen=True)
class SendVerdict:
    admitted: bool
    reason: str
    store_outcome: str | None


class StopGateAdmission:
    """Piece 7's admission of a send: the stop gate by lane, then the gate store for a piece no stop names.

    The store has no lane in its request, so it refuses every send of a stopped piece; the lane-aware answer for the
    stop's own cause lane is the stop gate's alone.
    """

    def __init__(self, rig: Rig) -> None:
        self._rig = rig

    def gate(self, lane: str) -> ModelStopGateResult:
        return stop_gate(
            ModelStopGateRequest(
                scope=STOP_SCOPE,
                stops=(self._rig.stop_record,) if self._rig.stop_record else (),
                policy=STOP_POLICY,
                now=NOW,
                lane_id=lane,
            ),
            FSM,
        )

    def decide(self, lane: str, step: str) -> SendVerdict:
        gate = self.gate(lane)
        if gate.outcome is EnumStopGateOutcome.REFUSE:
            return SendVerdict(False, gate.reason.value, None)
        if gate.reason is EnumStopGateReason.SWARM_LANE:
            return SendVerdict(True, gate.reason.value, None)
        sent = self._rig.send(PIECE_8, step)
        return SendVerdict(
            sent.outcome is EnumJoinJ6Outcome.SEND_ADMITTED,
            gate.reason.value,
            sent.store_outcome,
        )


class IgnoresStopAdmission(StopGateAdmission):
    """Mutation ``ignore_stop``: the stop gate does not read the stop, and every send is admitted."""

    def decide(self, lane: str, step: str) -> SendVerdict:
        return SendVerdict(True, EnumStopGateReason.NO_STOP.value, None)


class Rig:
    def __init__(self, *, mutation: str | None = None) -> None:
        assert mutation is None or mutation in MUTATIONS
        self.store = InMemoryGateStore(
            ModelGateStorePolicy(
                backend=EnumGateStoreBackend.IN_MEMORY,
                limits={PIECE_8: 3, PIECE_7: 3},
                reservation_ttl_seconds=60,
                order=(),
            )
        )
        self.join = HandlerGateStoreJoinJ6(
            self.store,
            InMemoryJoinJ6AuditSink(),
            InMemoryJoinJ6Projection(),
            load_join_j6_policy(),
        )
        self.api = (
            DeafInvalidationApi()
            if mutation == "deaf_invalidation"
            else InMemoryCheckRunApi()
        )
        self.sink = InMemoryEmissionSink()
        self.labcheck = HandlerLabCheckRunEffect(
            overlay=OV, api=self.api, sink=self.sink, clock=lambda: NOW
        )
        self.stop_record: ModelStopRecord | None = None
        self.admission: StopGateAdmission = (
            IgnoresStopAdmission(self)
            if mutation == "ignore_stop"
            else StopGateAdmission(self)
        )

    # -- piece 7: the stop and the sends
    def send(self, piece: str, step: str) -> ModelJoinJ6Result:
        return self.join.handle(
            ModelJoinJ6Request(
                correlation_id=f"cid-{step}",
                operation=EnumJoinJ6Operation.SEND,
                piece=piece,
                step_key=step,
                reservation_id=f"res-{step}",
                now=NOW.timestamp(),
            )
        )

    def open_stop_with_cause_lane(self) -> ModelStopRecord:
        committed = self.join.handle(
            ModelJoinJ6Request(
                correlation_id="cid-stop",
                operation=EnumJoinJ6Operation.STOP,
                piece=PIECE_8,
                step_key="stop-piece-8",
                now=NOW.timestamp(),
            )
        )
        assert committed.outcome is EnumJoinJ6Outcome.STOP_COMMITTED, committed
        opened = ModelStopRecord(
            id=STOP_ID,
            cls=STOP_CLASS,
            scope=STOP_SCOPE,
            state="open",
            stopped_at=STOPPED_AT,
        )
        self.stop_record = fold_stop_event(
            opened,
            ModelStopEvent.model_validate(
                {
                    "stop_id": STOP_ID,
                    "kind": EnumStopEventKind.LEASE_GRANTED,
                    "role": CONFIG.writer_roles[EnumStopEventKind.LEASE_GRANTED.value],
                    "at": LEASE_AT,
                    "lane_id": CAUSE_LANE,
                }
            ),
            FSM,
            CONFIG,
        ).stop
        assert self.stop_record.swarm_lane == CAUSE_LANE
        return self.stop_record

    # -- piece 8: the lab-bound job, run only when its send was admitted
    def post(
        self,
        *,
        attempt: str,
        current: str = CURRENT,
        head: str = HEAD,
        operation: EnumLabCheckRunOperation = EnumLabCheckRunOperation.POST,
        conclusion: EnumLabCheckRunConclusion
        | None = EnumLabCheckRunConclusion.SUCCESS,
    ) -> ModelLabCheckRunResult:
        post = operation is EnumLabCheckRunOperation.POST
        return self.labcheck.handle(
            ModelLabCheckRunRequest(
                correlation_id=f"cid-{operation.value}-{attempt}",
                operation=operation,
                repository=REPO,
                head_sha=head,
                current_attempt_id=current,
                dispatched_at=NOW
                - timedelta(seconds=OV.settings.wait_bound_seconds // 4),
                now=NOW,
                attempt_id=attempt if post else None,
                conclusion=conclusion if post else None,
                detail=f"{CAUSE_LANE} on {PIECE_8}",
            )
        )

    def read(
        self, *, current: str = CURRENT, head: str = HEAD
    ) -> ModelLabCheckRunResult:
        return self.post(
            attempt=current,
            current=current,
            head=head,
            operation=EnumLabCheckRunOperation.READ,
            conclusion=None,
        )

    def job(
        self,
        lane: str,
        step: str,
        *,
        attempt: str,
        current: str = CURRENT,
        head: str = HEAD,
    ) -> ModelLabCheckRunResult | None:
        """The lab-bound job of a send: it runs, and posts, only when its send is admitted."""
        if not self.admission.decide(lane, step).admitted:
            return None
        return self.post(attempt=attempt, current=current, head=head)


class PassAlreadyOnHeadError(AssertionError):
    """The test refuses a head that already reads PASS: a pass there would make the scenario vacuous."""


def require_head_without_pass(
    rig: Rig, *, current: str = CURRENT, head: str = HEAD
) -> None:
    verdict = rig.read(current=current, head=head).verdict
    if verdict is EnumLabCheckVerdict.PASS:
        raise PassAlreadyOnHeadError(f"{head} already holds a PASS for {current}")


# ---- the scenarios, as assertions; the mutation tests run them against a broken rig ----


def scenario_golden(rig: Rig) -> None:
    require_head_without_pass(rig)
    rig.open_stop_with_cause_lane()
    # the store alone refuses every send of the stopped piece, a send of another piece is admitted
    assert rig.send(PIECE_8, "store-alone").store_outcome == "refused_stop"
    assert rig.send(PIECE_7, "other-piece").outcome is EnumJoinJ6Outcome.SEND_ADMITTED

    cause = rig.admission.decide(CAUSE_LANE, "send-cause")
    assert cause.admitted, cause
    assert cause.reason == EnumStopGateReason.SWARM_LANE.value
    posted = rig.job(CAUSE_LANE, "send-cause", attempt=CURRENT)
    assert posted is not None
    assert posted.outcome is EnumLabCheckRunOutcome.POSTED, posted
    assert posted.posted is not None
    assert posted.posted.attempt_id == CURRENT
    assert identity_parts(posted.posted.attempt_id) == identity_parts(CURRENT)
    assert posted.posted.head_sha == HEAD
    assert posted.posted.name == OV.settings.check_name
    assert (posted.posted.status, posted.posted.conclusion) == ("completed", "success")
    assert rig.read().verdict is EnumLabCheckVerdict.PASS

    refused = rig.admission.decide(OTHER_LANE, "send-other")
    assert not refused.admitted, refused
    assert refused.reason == EnumStopGateReason.STOP_OPEN.value
    calls = rig.api.calls
    assert rig.job(OTHER_LANE, "send-other", attempt=OLD, head=OLD_HEAD) is None
    assert rig.api.calls == calls, "a refused send makes no call on the check-run API"
    assert (
        rig.api.list_runs(
            repository=REPO, head_sha=OLD_HEAD, check_name=OV.settings.check_name
        )
        == ()
    )
    assert [r.attempt_id for r in rig.api.runs] == [CURRENT], (
        "only the cause lane's run exists"
    )


def scenario_error(rig: Rig) -> None:
    rig.open_stop_with_cause_lane()
    # the earlier attempt was current when its job ran, and left a counting pass on the head
    earlier = rig.job(CAUSE_LANE, "send-earlier", attempt=OLD, current=OLD)
    assert earlier is not None
    assert earlier.outcome is EnumLabCheckRunOutcome.POSTED, earlier
    (earlier_run,) = rig.api.runs
    assert (earlier_run.attempt_id, earlier_run.conclusion) == (OLD, "success")

    # the order moves on; the superseded attempt's late post posts nothing and takes its own pass away
    late = rig.job(CAUSE_LANE, "send-late", attempt=OLD, current=CURRENT)
    assert late is not None
    assert late.outcome is EnumLabCheckRunOutcome.REFUSED_SUPERSEDED_ATTEMPT, late
    assert late.posted is None
    assert late.invalidated == (earlier_run.check_run_id,)
    assert len(rig.api.runs) == 1, "the superseded post created no run"
    assert rig.api.runs[0].conclusion == OV.settings.superseded_conclusion
    assert rig.read().verdict is EnumLabCheckVerdict.SUPERSEDED

    # a head that already reads PASS is refused by the test
    passing = Rig()
    passing.api.create_run(
        repository=REPO,
        head_sha=HEAD,
        name=OV.settings.check_name,
        attempt_id=CURRENT,
        status="completed",
        conclusion="success",
        title="",
        summary="",
    )
    with pytest.raises(PassAlreadyOnHeadError):
        require_head_without_pass(passing)

    # the stop still refuses a send of another lane of piece 8, and it posts nothing
    refused = rig.admission.decide(OTHER_LANE, "send-other")
    assert not refused.admitted, refused
    assert rig.job(OTHER_LANE, "send-other", attempt=OLD, head=OLD_HEAD) is None
    assert (
        rig.api.list_runs(
            repository=REPO, head_sha=OLD_HEAD, check_name=OV.settings.check_name
        )
        == ()
    )


# ---- golden ----


def test_golden_cause_lane_is_admitted_past_the_stop_and_its_check_reads_pass_on_the_head() -> (
    None
):
    scenario_golden(Rig())


def test_golden_the_chain_of_the_post_closes_with_a_start_and_a_success() -> None:
    rig = Rig()
    rig.open_stop_with_cause_lane()
    assert rig.job(CAUSE_LANE, "send-cause", attempt=CURRENT) is not None
    assert [(e.phase, e.failure_code) for e in rig.sink.events] == [
        ("start", None),
        ("success", None),
    ]


def test_golden_with_no_stop_open_both_lanes_are_admitted_through_the_store() -> None:
    rig = Rig()
    for lane, step in ((CAUSE_LANE, "send-a"), (OTHER_LANE, "send-b")):
        verdict = rig.admission.decide(lane, step)
        assert verdict.admitted
        assert verdict.reason == EnumStopGateReason.NO_STOP.value
        assert verdict.store_outcome == "ok"


# ---- error ----


def test_error_superseded_attempt_posts_nothing_and_its_counting_run_is_invalidated() -> (
    None
):
    scenario_error(Rig())


def test_error_the_test_refuses_a_head_that_already_holds_a_pass() -> None:
    rig = Rig()
    rig.api.create_run(
        repository=REPO,
        head_sha=HEAD,
        name=OV.settings.check_name,
        attempt_id=CURRENT,
        status="completed",
        conclusion="success",
        title="",
        summary="",
    )
    with pytest.raises(PassAlreadyOnHeadError):
        scenario_golden(rig)
    assert rig.stop_record is None, "the refusal comes before the stop is opened"


def test_error_handler_has_no_refusal_for_a_second_pass_of_the_current_attempt() -> (
    None
):
    """Pins what exists: the handler answers a repeated pass as ``posted`` on the same run, never as a refusal."""
    rig = Rig()
    rig.open_stop_with_cause_lane()
    first = rig.job(CAUSE_LANE, "send-1", attempt=CURRENT)
    again = rig.job(CAUSE_LANE, "send-2", attempt=CURRENT)
    assert first is not None
    assert again is not None
    assert again.outcome is EnumLabCheckRunOutcome.POSTED
    assert again.invalidated == ()
    assert len(rig.api.runs) == 1
    assert again.posted is not None
    assert first.posted is not None
    assert again.posted.check_run_id == first.posted.check_run_id
    assert rig.read().verdict is EnumLabCheckVerdict.PASS


def test_error_a_pass_of_an_earlier_attempt_is_invalidated_by_the_current_attempts_post() -> (
    None
):
    rig = Rig()
    rig.api.create_run(
        repository=REPO,
        head_sha=HEAD,
        name=OV.settings.check_name,
        attempt_id=OLD,
        status="completed",
        conclusion="success",
        title="",
        summary="",
    )
    rig.open_stop_with_cause_lane()
    result = rig.job(CAUSE_LANE, "send-cause", attempt=CURRENT)
    assert result is not None
    assert result.outcome is EnumLabCheckRunOutcome.POSTED
    assert len(result.invalidated) == 1
    by_attempt = {r.attempt_id: r for r in rig.api.runs}
    assert by_attempt[OLD].conclusion == OV.settings.superseded_conclusion
    assert by_attempt[CURRENT].conclusion == "success"


def test_error_a_refused_send_of_the_stopped_piece_is_refused_with_the_stop_open_reason() -> (
    None
):
    rig = Rig()
    rig.open_stop_with_cause_lane()
    verdict = rig.admission.decide(OTHER_LANE, "send-other")
    assert not verdict.admitted
    assert verdict.reason == EnumStopGateReason.STOP_OPEN.value
    assert verdict.store_outcome is None, "the store is never asked for a refused lane"
    assert rig.api.runs == ()
    assert rig.sink.events == []


# ---- mutation ----


def test_mutation_ignore_stop_reddens_the_golden_scenario() -> None:
    scenario_golden(Rig())  # control: the same scenario passes without the mutation
    with pytest.raises(AssertionError):
        scenario_golden(Rig(mutation="ignore_stop"))


def test_mutation_ignore_stop_reddens_the_error_scenario() -> None:
    scenario_error(Rig())  # control: the same scenario passes without the mutation
    with pytest.raises(AssertionError):
        scenario_error(Rig(mutation="ignore_stop"))


def test_mutation_deaf_invalidation_reddens_the_error_scenario() -> None:
    scenario_error(Rig())  # control
    with pytest.raises(AssertionError):
        scenario_error(Rig(mutation="deaf_invalidation"))
    deaf = Rig(mutation="deaf_invalidation")
    deaf.open_stop_with_cause_lane()
    deaf.job(CAUSE_LANE, "send-earlier", attempt=OLD, current=OLD)
    late = deaf.job(CAUSE_LANE, "send-late", attempt=OLD, current=CURRENT)
    assert late is not None
    assert late.outcome is EnumLabCheckRunOutcome.FAILED_INVALIDATION_NOT_READ


def test_mutation_ignore_stop_posts_for_the_lane_the_stop_refuses() -> None:
    rig = Rig(mutation="ignore_stop")
    rig.open_stop_with_cause_lane()
    posted = rig.job(OTHER_LANE, "send-other", attempt=OLD, head=OLD_HEAD)
    assert posted is not None, "the broken gate lets the refused lane post"
    assert rig.api.calls > 0
