# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the host install state reducer, with the failing controls (OMN-23957).

* golden: request, snapshot, install, verify against the render, run reads ``run_ok`` and the record holds the digests, actor
  and times of every step;
* error: a verify read of another file fails the record (never ``verified``) and the rollback that restores the snapshot
  reads ``rolled_back``; a rollback that restores another file stays ``failed``; out-of-order, foreign and finished steps are
  refused with a typed error;
* control: a naive fold that trusts the verify step (no digest compare) reads ``verified`` for the hand-edited file, and the
  shared assertion turns red on it, so the mismatch test is not vacuous.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest

from omnimarket.nodes.node_host_install_state_reducer.handlers.handler_host_install_state import (
    HandlerHostInstallState,
    HostInstallTransitionError,
    fold,
)
from omnimarket.nodes.node_host_install_state_reducer.models import (
    EnumHostInstallState,
    ModelHostInstallRecord,
    ModelHostInstallReduceCommand,
)
from tests.nodes.node_host_install_state_reducer.support import (
    HOST,
    OTHER,
    PREVIOUS,
    RENDER,
    RUN_ID,
    START,
    UNIT,
    O,
    S,
    error_chain,
    event,
    fold_all,
    golden_chain,
    overlay,
    request,
)

pytestmark = pytest.mark.unit

ST = EnumHostInstallState


def test_golden_chain_reads_run_ok_with_digests_actor_and_times() -> None:
    record = fold_all(golden_chain())
    assert record.state is ST.RUN_OK
    assert (record.host, record.unit, record.correlation_id) == (HOST, UNIT, RUN_ID)
    assert record.render_digest == record.installed_digest == RENDER
    assert record.rollback_target_digest == PREVIOUS
    assert record.requested_by == record.last_actor == "actor-a"
    assert record.requested_at == START
    assert record.updated_at == START + timedelta(minutes=5)
    assert [h.state_after for h in record.history] == [
        ST.REQUESTED,
        ST.SNAPSHOTTED,
        ST.INSTALLING,
        ST.INSTALLED,
        ST.VERIFIED,
        ST.RUN_OK,
    ]


def test_golden_chain_each_step_reads_its_state() -> None:
    record = None
    states = []
    for item in golden_chain():
        record = fold(record, item, overlay())
        states.append(record.state)
    assert states == [
        ST.REQUESTED,
        ST.SNAPSHOTTED,
        ST.INSTALLING,
        ST.INSTALLED,
        ST.VERIFIED,
        ST.RUN_OK,
    ]


def assert_never_verified_on_mismatch(record: ModelHostInstallRecord) -> None:
    assert record.state is not ST.VERIFIED
    assert record.state is ST.FAILED
    assert record.installed_digest == OTHER
    assert record.failure_detail == "digest_mismatch"


def test_error_chain_mismatch_fails_then_rollback_restores_the_snapshot() -> None:
    chain = error_chain()
    failed = fold_all(chain[:4])
    assert_never_verified_on_mismatch(failed)
    assert failed.rollback_target_digest == PREVIOUS
    rolled_back = fold_all(chain[4:], failed)
    assert rolled_back.state is ST.ROLLED_BACK
    assert rolled_back.installed_digest == PREVIOUS
    assert rolled_back.history[-1].actor == "actor-a"


def test_control_a_naive_fold_reads_verified_for_the_hand_edited_file() -> None:
    """The failing control: trust the verify step with no compare and the shared assertion turns red."""
    chain = error_chain()
    installed = fold_all(chain[:3])
    verify = chain[3]
    naive = installed.model_copy(
        update={"state": ST.VERIFIED, "installed_digest": verify.installed_digest}
    )
    with pytest.raises(AssertionError):
        assert_never_verified_on_mismatch(naive)
    assert_never_verified_on_mismatch(fold_all([verify], installed))


def test_rollback_that_restores_another_file_stays_failed() -> None:
    failed = fold_all(error_chain()[:4])
    after = fold_all([event(S.ROLLBACK, minute=4, installed_digest=OTHER)], failed)
    assert after.state is ST.FAILED
    assert after.failure_detail == "rollback_digest_mismatch"
    assert after.rollback_failures == 1
    again = fold_all(
        [event(S.ROLLBACK, O.OK, minute=5, installed_digest=PREVIOUS)], after
    )
    assert again.state is ST.ROLLED_BACK


def test_rollback_with_no_previous_file_accepts_any_read() -> None:
    chain = [
        request(),
        event(S.SNAPSHOT, minute=1),
        event(S.INSTALL, minute=2, installed_digest=RENDER),
        event(S.VERIFY, minute=3, installed_digest=OTHER),
    ]
    failed = fold_all(chain)
    assert failed.rollback_target_digest == ""
    assert fold_all([event(S.ROLLBACK, minute=4)], failed).state is ST.ROLLED_BACK


def test_failed_rollback_step_is_counted_and_stays_failed() -> None:
    failed = fold_all(error_chain()[:4])
    after = fold_all(
        [event(S.ROLLBACK, O.FAILED, minute=4, detail="copy refused")], failed
    )
    assert (after.state, after.rollback_failures, after.failure_detail) == (
        ST.FAILED,
        1,
        "copy refused",
    )


@pytest.mark.parametrize(
    ("chain_length", "failing", "detail"),
    [
        (1, S.SNAPSHOT, "snapshot_failed"),
        (2, S.INSTALL, "install_failed"),
        (3, S.INSTALL, "install_failed"),
        (4, S.VERIFY, "verify_failed"),
        (5, S.RUN, "run_failed"),
    ],
)
def test_a_failed_step_at_the_expected_step_fails_the_record(
    chain_length: int, failing: S, detail: str
) -> None:
    prior = fold_all(golden_chain()[:chain_length])
    failed = fold_all([event(failing, O.FAILED, minute=9)], prior)
    assert failed.state is ST.FAILED
    assert failed.failure_detail == detail


def test_a_new_request_after_a_finished_or_failed_install_starts_a_new_record() -> None:
    done = fold_all(golden_chain())
    again_id = uuid4()
    fresh = fold_all([request(correlation_id=again_id, minute=30)], done)
    assert (fresh.state, fresh.correlation_id, fresh.rollback_target_digest) == (
        ST.REQUESTED,
        again_id,
        "",
    )
    failed = fold_all(error_chain()[:4])
    assert fold_all([request(minute=30)], failed).state is ST.REQUESTED


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param(lambda: [event(S.SNAPSHOT)], id="step-with-no-request"),
        pytest.param(
            lambda: [request(), event(S.VERIFY, installed_digest=RENDER)],
            id="verify-before-install",
        ),
        pytest.param(lambda: [request(), event(S.RUN)], id="run-before-verify"),
        pytest.param(
            lambda: [request(), request(minute=1)], id="request-while-in-flight"
        ),
        pytest.param(
            lambda: [request(), event(S.SNAPSHOT, O.STARTED, minute=1)],
            id="snapshot-started",
        ),
        pytest.param(
            lambda: [request(), event(S.ROLLBACK, minute=1)],
            id="rollback-of-a-healthy-install",
        ),
        pytest.param(
            lambda: [request(render_digest="")], id="request-with-no-render-digest"
        ),
        pytest.param(lambda: [request(render_digest="ab12")], id="short-digest"),
        pytest.param(
            lambda: [request(), event(S.SNAPSHOT, minute=1, host="host-b")],
            id="other-host",
        ),
        pytest.param(
            lambda: [request(), event(S.SNAPSHOT, minute=1, unit="unit-b")],
            id="other-unit",
        ),
        pytest.param(
            lambda: [request(), event(S.SNAPSHOT, minute=1, correlation_id=uuid4())],
            id="other-install",
        ),
        pytest.param(
            lambda: [request(), event(S.REQUEST, O.FAILED, minute=1)],
            id="failed-request",
        ),
    ],
)
def test_error_chain_refuses_with_a_typed_error(bad) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(HostInstallTransitionError) as refusal:
        fold_all(bad())
    assert HOST in str(refusal.value) or "host-b" in str(refusal.value)


def test_error_chain_a_finished_install_refuses_every_step_but_a_request() -> None:
    done = fold_all(golden_chain())
    for step in (S.SNAPSHOT, S.INSTALL, S.VERIFY, S.RUN, S.ROLLBACK):
        with pytest.raises(HostInstallTransitionError):
            fold_all([event(step, minute=9, installed_digest=RENDER)], done)
    rolled = fold_all(error_chain())
    with pytest.raises(HostInstallTransitionError):
        fold_all([event(S.ROLLBACK, minute=9, installed_digest=PREVIOUS)], rolled)


def test_verify_read_without_a_digest_is_refused() -> None:
    prior = fold_all(golden_chain()[:4])
    with pytest.raises(HostInstallTransitionError):
        fold_all([event(S.VERIFY, minute=9)], prior)


def test_history_and_detail_follow_the_overlay_bounds() -> None:
    small = overlay().model_copy(update={"history_limit": 2, "detail_max_chars": 5})
    record = None
    for item in golden_chain():
        record = fold(record, item.model_copy(update={"detail": "0123456789"}), small)
    assert record is not None
    assert len(record.history) == 2
    assert record.history[-1].state_after is ST.RUN_OK
    assert all(h.detail == "01234" for h in record.history)
    wide = overlay().model_copy(update={"digest_hex_length": 8})
    with pytest.raises(HostInstallTransitionError):
        fold(None, request(), wide)


def test_handler_returns_the_record_as_the_reducer_projection() -> None:
    handler = HandlerHostInstallState(overlay())
    output = asyncio.run(
        handler.handle(ModelHostInstallReduceCommand(prior=None, event=request()))
    )
    (projection,) = output.projections
    assert isinstance(projection, ModelHostInstallRecord)
    assert projection.state is ST.REQUESTED


def test_handler_without_an_overlay_refuses_naming_the_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    handler = HandlerHostInstallState()
    with pytest.raises(ValueError, match="node_host_install_state_reducer"):
        asyncio.run(handler.handle(ModelHostInstallReduceCommand(event=request())))
