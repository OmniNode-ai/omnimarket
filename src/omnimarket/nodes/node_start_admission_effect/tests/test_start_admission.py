# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Start admission (OMN-22939): observe vs enforce, a single-use reservation across an epoch, and no answer.

Every scenario runs against the real in-memory gate store; the doubles only wrap it to advance its epoch at a
chosen moment or to withhold its answer.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.model_gate_store import (
    EnumGateStoreOperation,
    EnumGateStoreOutcome,
    ModelGateStorePolicy,
    ModelGateStoreRequest,
    ModelGateStoreResult,
)
from omnimarket.models.model_start_admission import (
    EnumAdmissionMode,
    EnumAdmissionOutcome,
    ModelStartAdmissionPolicy,
    ModelStartAdmissionRequest,
)
from omnimarket.nodes.node_gate_store_effect.handlers.store_in_memory import (
    InMemoryGateStore,
)
from omnimarket.nodes.node_start_admission_effect.handlers.adapter_log_in_memory import (
    InMemoryStartAdmissionLog,
)
from omnimarket.nodes.node_start_admission_effect.handlers.handler_start_admission import (
    HandlerStartAdmission,
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_start_admission_effect.overlay import (
    load_start_admission_policy,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

STORE_POLICY = ModelGateStorePolicy.model_validate(
    {
        "backend": "in_memory",
        "limits": {"alpha": 1, "beta": 1},
        "reservation_ttl_seconds": 60,
        "order": ["alpha", "beta"],
    }
)


def policy(
    default: EnumAdmissionMode = EnumAdmissionMode.OBSERVE,
    modes: dict[str, EnumAdmissionMode] | None = None,
    deadline: float = 5.0,
    redecisions: int = 2,
) -> ModelStartAdmissionPolicy:
    return ModelStartAdmissionPolicy(
        default_mode=default,
        modes=modes or {},
        answer_deadline_seconds=deadline,
        max_redecisions=redecisions,
    )


def request(
    key: str = "run1/start", rid: str = "r1", **fields: object
) -> ModelStartAdmissionRequest:
    base: dict[str, object] = {
        "correlation_id": f"c-{key}",
        "step_key": key,
        "path": "lane_start",
        "piece": "alpha",
        "reservation_id": rid,
        "now": 1.0,
    }
    return ModelStartAdmissionRequest.model_validate({**base, **fields})


def store_call(
    op: EnumGateStoreOperation, key: str, **fields: object
) -> ModelGateStoreRequest:
    return ModelGateStoreRequest.model_validate(
        {"correlation_id": key, "operation": op, "step_key": key, "now": 1.0, **fields}
    )


class EpochAdvancer:
    """Wraps the real store; advances its epoch just before each of the first ``times`` consumes."""

    def __init__(self, inner: InMemoryGateStore, times: int) -> None:
        self.inner = inner
        self.remaining = times
        self.ops: list[str] = []

    def transact(self, req: ModelGateStoreRequest) -> ModelGateStoreResult:
        self.ops.append(req.operation.value)
        if req.operation is EnumGateStoreOperation.CONSUME and self.remaining > 0:
            self.remaining -= 1
            self.inner.transact(
                store_call(
                    EnumGateStoreOperation.RECORD_SAFETY,
                    f"advance-{self.remaining}",
                    piece="beta",
                    safety_state="clear",
                )
            )
        return self.inner.transact(req)


class SilentStore:
    """A store that gives no answer until released, or raises."""

    def __init__(self, raises: bool = False) -> None:
        self.release = threading.Event()
        self.raises = raises

    def transact(self, req: ModelGateStoreRequest) -> ModelGateStoreResult:
        if self.raises:
            raise RuntimeError("store down")
        self.release.wait(timeout=10)
        raise RuntimeError("released after the deadline")


def stopped_store() -> InMemoryGateStore:
    store = InMemoryGateStore(STORE_POLICY)
    store.transact(
        store_call(
            EnumGateStoreOperation.RECORD_SAFETY,
            "stop-alpha",
            piece="alpha",
            safety_state="stop",
        )
    )
    return store


# ---- AC1: observe vs enforce ----


def test_observe_vs_enforce_a_refusal_is_logged_and_started_in_observe_and_refused_in_enforce() -> (
    None
):
    observe_log, enforce_log = InMemoryStartAdmissionLog(), InMemoryStartAdmissionLog()
    observed = HandlerStartAdmission(
        stopped_store(), observe_log, policy(EnumAdmissionMode.OBSERVE)
    ).handle(request())
    enforced = HandlerStartAdmission(
        stopped_store(), enforce_log, policy(EnumAdmissionMode.ENFORCE)
    ).handle(request())

    assert observed.outcome is EnumAdmissionOutcome.OBSERVED_REFUSAL
    assert observed.proceed is True
    assert observed.reason == EnumGateStoreOutcome.REFUSED_STOP.value
    assert enforced.outcome is EnumAdmissionOutcome.REFUSED
    assert enforced.proceed is False
    assert enforced.reason == EnumGateStoreOutcome.REFUSED_STOP.value
    (obs_row,) = observe_log.rows()
    (enf_row,) = enforce_log.rows()
    assert (obs_row.mode, obs_row.outcome, obs_row.proceed) == (
        EnumAdmissionMode.OBSERVE,
        EnumAdmissionOutcome.OBSERVED_REFUSAL,
        True,
    )
    assert (enf_row.mode, enf_row.outcome, enf_row.proceed) == (
        EnumAdmissionMode.ENFORCE,
        EnumAdmissionOutcome.REFUSED,
        False,
    )


def test_observe_vs_enforce_a_request_the_policy_admits_starts_in_both_modes() -> None:
    for mode in EnumAdmissionMode:
        log = InMemoryStartAdmissionLog()
        result = HandlerStartAdmission(
            InMemoryGateStore(STORE_POLICY), log, policy(mode)
        ).handle(request())
        assert result.outcome is EnumAdmissionOutcome.ADMITTED
        assert result.proceed is True
        assert [r.outcome for r in log.rows()] == [EnumAdmissionOutcome.ADMITTED]


def test_observe_vs_enforce_the_mode_is_per_path_and_unnamed_paths_take_the_default() -> (
    None
):
    pol = policy(
        EnumAdmissionMode.OBSERVE, {"session_start": EnumAdmissionMode.ENFORCE}
    )
    handler = HandlerStartAdmission(stopped_store(), InMemoryStartAdmissionLog(), pol)
    named = handler.handle(request("k1", "r1", path="session_start"))
    unnamed = handler.handle(request("k2", "r2", path="lane_start"))
    assert (named.mode, named.proceed) == (EnumAdmissionMode.ENFORCE, False)
    assert (unnamed.mode, unnamed.proceed) == (EnumAdmissionMode.OBSERVE, True)


def test_observe_vs_enforce_a_limit_refusal_is_what_would_be_refused() -> None:
    store = InMemoryGateStore(STORE_POLICY)
    log = InMemoryStartAdmissionLog()
    handler = HandlerStartAdmission(store, log, policy(EnumAdmissionMode.ENFORCE))
    first = handler.handle(request("k1", "r1"))
    second = handler.handle(request("k2", "r2"))
    assert first.outcome is EnumAdmissionOutcome.ADMITTED
    assert second.outcome is EnumAdmissionOutcome.REFUSED
    assert second.reason == EnumGateStoreOutcome.REFUSED_LIMIT.value


def test_observe_vs_enforce_a_replayed_step_key_is_not_a_second_start() -> None:
    handler = HandlerStartAdmission(
        InMemoryGateStore(STORE_POLICY),
        InMemoryStartAdmissionLog(),
        policy(EnumAdmissionMode.ENFORCE),
    )
    assert handler.handle(request()).proceed is True
    replay = handler.handle(request())
    assert replay.proceed is False
    assert replay.reason == EnumGateStoreOutcome.DUPLICATE.value


# ---- AC2: a reservation is single use, never consumed after an epoch advance, released while re-deciding ----


def test_reservation_epoch_is_single_use() -> None:
    store = InMemoryGateStore(STORE_POLICY)
    HandlerStartAdmission(
        store, InMemoryStartAdmissionLog(), policy(EnumAdmissionMode.ENFORCE)
    ).handle(request())
    again = store.transact(
        store_call(EnumGateStoreOperation.CONSUME, "again", reservation_id="r1")
    )
    assert again.outcome is EnumGateStoreOutcome.REFUSED_CONSUMED


def test_reservation_epoch_advance_before_the_consume_releases_and_re_decides() -> None:
    inner = InMemoryGateStore(STORE_POLICY)
    store = EpochAdvancer(inner, times=1)
    log = InMemoryStartAdmissionLog()
    result = HandlerStartAdmission(
        store, log, policy(EnumAdmissionMode.ENFORCE)
    ).handle(request())

    # alpha's limit is 1: the second reserve only succeeds because the first was released.
    assert store.ops == ["reserve", "consume", "release", "reserve", "consume"]
    assert result.outcome is EnumAdmissionOutcome.ADMITTED
    assert result.attempts == 2
    assert (
        inner.transact(
            store_call(EnumGateStoreOperation.READ, "read", piece="alpha")
        ).counted
        == 1
    )
    (row,) = log.rows()
    assert row.attempts == 2


def test_reservation_epoch_is_never_consumed_after_the_epoch_advanced() -> None:
    inner = InMemoryGateStore(STORE_POLICY)
    reserved = inner.transact(
        store_call(
            EnumGateStoreOperation.RESERVE, "res", piece="alpha", reservation_id="r9"
        )
    )
    inner.transact(
        store_call(
            EnumGateStoreOperation.RECORD_SAFETY,
            "adv",
            piece="beta",
            safety_state="clear",
        )
    )
    stale = inner.transact(
        store_call(
            EnumGateStoreOperation.CONSUME,
            "stale",
            reservation_id="r9",
            expected_epoch=reserved.epoch,
        )
    )
    assert stale.outcome is EnumGateStoreOutcome.REFUSED_FENCE
    current = inner.transact(
        store_call(
            EnumGateStoreOperation.CONSUME,
            "current",
            reservation_id="r9",
            expected_epoch=stale.epoch,
        )
    )
    assert current.outcome is EnumGateStoreOutcome.OK


def test_reservation_epoch_a_decision_that_never_settles_reads_as_a_refusal() -> None:
    inner = InMemoryGateStore(STORE_POLICY)
    store = EpochAdvancer(inner, times=99)
    observe = HandlerStartAdmission(
        store,
        InMemoryStartAdmissionLog(),
        policy(EnumAdmissionMode.OBSERVE, redecisions=1),
    ).handle(request("k1", "r1"))
    assert observe.outcome is EnumAdmissionOutcome.OBSERVED_REFUSAL
    assert observe.reason == "redecision_exhausted"
    assert observe.attempts == 2
    enforce = HandlerStartAdmission(
        store,
        InMemoryStartAdmissionLog(),
        policy(EnumAdmissionMode.ENFORCE, redecisions=1),
    ).handle(request("k2", "r2"))
    assert (enforce.outcome, enforce.proceed) == (EnumAdmissionOutcome.REFUSED, False)
    # every reservation the exhausted decisions made was released: nothing is left counted.
    assert (
        inner.transact(
            store_call(EnumGateStoreOperation.READ, "read", piece="alpha")
        ).counted
        == 0
    )


# ---- AC3: no answer by the deadline ----


@pytest.mark.parametrize("mode", list(EnumAdmissionMode))
def test_no_answer_a_start_proceeds_logged_as_unadmitted(
    mode: EnumAdmissionMode,
) -> None:
    store, log = SilentStore(), InMemoryStartAdmissionLog()
    try:
        result = HandlerStartAdmission(store, log, policy(mode, deadline=0.05)).handle(
            request()
        )
    finally:
        store.release.set()
    assert result.outcome is EnumAdmissionOutcome.UNADMITTED
    assert result.proceed is True
    (row,) = log.rows()
    assert (row.outcome, row.proceed, row.reason) == (
        EnumAdmissionOutcome.UNADMITTED,
        True,
        "no_answer",
    )


@pytest.mark.parametrize("mode", list(EnumAdmissionMode))
def test_no_answer_an_outward_action_does_not_proceed(mode: EnumAdmissionMode) -> None:
    store, log = SilentStore(), InMemoryStartAdmissionLog()
    try:
        result = HandlerStartAdmission(store, log, policy(mode, deadline=0.05)).handle(
            request(outward=True)
        )
    finally:
        store.release.set()
    assert result.outcome is EnumAdmissionOutcome.NOT_PROCEEDED
    assert result.proceed is False
    (row,) = log.rows()
    assert (row.outcome, row.proceed) == (EnumAdmissionOutcome.NOT_PROCEEDED, False)


def test_no_answer_a_store_that_raises_is_no_answer() -> None:
    log = InMemoryStartAdmissionLog()
    handler = HandlerStartAdmission(
        SilentStore(raises=True), log, policy(EnumAdmissionMode.ENFORCE)
    )
    inward = handler.handle(request("k1", "r1"))
    outward = handler.handle(request("k2", "r2", outward=True))
    assert (inward.outcome, inward.proceed) == (EnumAdmissionOutcome.UNADMITTED, True)
    assert (outward.outcome, outward.proceed) == (
        EnumAdmissionOutcome.NOT_PROCEEDED,
        False,
    )
    assert inward.reason == "store_fault:RuntimeError"


# ---- overlay and bus edge ----


def test_overlay_loads_every_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fields = {
        "default_mode": "observe",
        "modes": {"session_start": "enforce"},
        "answer_deadline_seconds": 2.5,
        "max_redecisions": 3,
    }
    install_node_overlay(monkeypatch, tmp_path, "start_admission", fields)
    loaded = load_start_admission_policy()
    assert loaded.modes == {"session_start": EnumAdmissionMode.ENFORCE}
    assert loaded.answer_deadline_seconds == 2.5
    assert loaded.max_redecisions == 3


@pytest.mark.parametrize(
    "change",
    [
        {"default_mode": "off"},
        {"answer_deadline_seconds": 0},
        {"max_redecisions": -1},
        {"modes": {"x": "sometimes"}},
    ],
    ids=["unknown_mode", "zero_deadline", "negative_redecisions", "unknown_path_mode"],
)
def test_overlay_refuses_a_bad_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, object]
) -> None:
    fields: dict[str, object] = {
        "default_mode": "observe",
        "modes": {},
        "answer_deadline_seconds": 1.0,
        "max_redecisions": 1,
        **change,
    }
    install_node_overlay(monkeypatch, tmp_path, "start_admission", fields)
    with pytest.raises(NodeOverlayError):
        load_start_admission_policy()


def test_bus_edge_answers_a_request_and_a_bad_payload_with_typed_events() -> None:
    topics = load_topics()
    handler = HandlerStartAdmission(
        InMemoryGateStore(STORE_POLICY),
        InMemoryStartAdmissionLog(),
        policy(EnumAdmissionMode.ENFORCE),
    )
    ok = answer_command(request().model_dump_json().encode(), handler, topics)
    bad = answer_command(b'{"nope": 1}', handler, topics)
    assert [t for t, _ in ok] == [topics["start_admission_answered"]]
    assert [t for t, _ in bad] == [topics["start_admission_failed"]]
    assert topics["start_admission_answered"].startswith("onex.evt.")
