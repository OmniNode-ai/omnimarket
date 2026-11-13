# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J5 terminal: the join closes its chain typed on every path out of the run (OMN-23591).

The golden case writes start then success and the floor reads golden_only. The error cases
(an append that raises, a projection that raises, a replay, a deadline) write start then failure
with the typed code and the floor reads no start_without_terminal. A failing control proves the
tests can fail: a sink that drops failure events leaves a start without a terminal.
"""

from __future__ import annotations

import signal
import threading
import time
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.work_ledger_append import EnumJoinJ5Outcome
from omnimarket.nodes.node_work_ledger_append_effect.overlay_join_j5 import (
    ModelJoinJ5Overlay,
)
from tests.nodes.node_factory_floor_publish_effect.limits_support import TEST_LIMITS
from tests.unit.nodes.node_work_ledger_append_effect.test_join_j5 import (
    CHAIN_DEADLINE_S,
    NOW,
    POLICY,
    _fold,
    _request,
    _Rig,
)

pytestmark = pytest.mark.unit

# The floor reads a start with no terminal only once the chain deadline has passed.
LATER = NOW + timedelta(seconds=CHAIN_DEADLINE_S + 100)


def test_golden_a_normal_run_writes_start_then_success_and_the_floor_reads_golden_only() -> (
    None
):
    rig = _Rig()
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)
    rig.join.handle(_request(cid))

    mine = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert [e.phase for e in mine] == ["start", "success"]
    state, golden, findings = _fold(rig.sink.events, LATER)
    assert (state, findings) == ("golden_only", [])
    assert golden == 1


def test_error_the_append_raises_after_the_start_and_the_chain_closes_typed() -> None:
    rig = _Rig()
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)

    def _boom(request, *, writer_principal=None):
        raise RuntimeError("boom")

    rig.append.handle = _boom
    result = rig.join.handle(_request(cid))

    assert result.outcome is EnumJoinJ5Outcome.INTERRUPTED
    assert result.failure_code == POLICY.interrupted_failure_code
    mine = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert [e.phase for e in mine] == ["start", "failure"]
    assert mine[-1].failure_code == POLICY.interrupted_failure_code
    _, _, findings = _fold(rig.sink.events, LATER)
    assert "start_without_terminal" not in findings


def test_error_the_projection_raises_after_the_append_landed_and_the_chain_closes_typed() -> (
    None
):
    rig = _Rig()
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)

    def _boom(self, *, ledger_id, ledger_seq, raw_row=None, correlation_id=None):
        raise OSError("projection blew up")

    rig.projection.read_row = _boom.__get__(rig.projection)
    result = rig.join.handle(_request(cid))

    assert result.outcome is EnumJoinJ5Outcome.INTERRUPTED
    assert result.failure_code == POLICY.interrupted_failure_code
    mine = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert [e.phase for e in mine] == ["start", "failure"]
    assert mine[-1].failure_code == POLICY.interrupted_failure_code
    _, _, findings = _fold(rig.sink.events, LATER)
    assert "start_without_terminal" not in findings


def test_error_a_replay_writes_no_second_start_and_no_second_terminal() -> None:
    rig = _Rig()
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)
    request = _request(cid)
    rig.join.handle(request)

    def _boom(request, *, writer_principal=None):
        raise RuntimeError("boom")

    rig.append.handle = _boom
    result = rig.join.handle(request)

    assert result.outcome is EnumJoinJ5Outcome.INTERRUPTED
    mine = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert [e.phase for e in mine] == ["start", "success"]


def test_error_deadline_a_blocking_call_is_interrupted_and_the_chain_closes_typed() -> (
    None
):
    from omnimarket.nodes.node_work_ledger_append_effect.adapters.run_deadline import (
        run_deadline,
    )

    rig = _Rig()
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)

    def _block(request, *, writer_principal=None):
        time.sleep(10)

    rig.append.handle = _block
    t0 = time.monotonic()
    with run_deadline(1):
        result = rig.join.handle(_request(cid))
    elapsed = time.monotonic() - t0

    assert elapsed < 5
    assert result.outcome is EnumJoinJ5Outcome.DEADLINE_EXCEEDED
    assert result.failure_code == POLICY.deadline_exceeded_failure_code
    mine = [e for e in rig.sink.events if e.piece == POLICY.to_piece]
    assert [e.phase for e in mine] == ["start", "failure"]
    assert mine[-1].failure_code == POLICY.deadline_exceeded_failure_code
    _, _, findings = _fold(rig.sink.events, LATER)
    assert "start_without_terminal" not in findings


def test_control_a_sink_that_drops_failure_events_leaves_start_without_terminal() -> (
    None
):
    class _DroppingSink:
        """Injected break: the typed failure never reaches the journal."""

        def __init__(self, inner) -> None:
            self._inner = inner

        def emit(self, emission) -> None:
            if emission.phase != "failure":
                self._inner.emit(emission)

    rig = _Rig()
    cid = uuid4().hex
    rig.upstream_chain(cid, resolves=True)
    rig.join._sink = _DroppingSink(rig.sink)

    def _boom(request, *, writer_principal=None):
        raise RuntimeError("boom")

    rig.append.handle = _boom
    rig.join.handle(_request(cid))

    _, _, findings = _fold(rig.sink.events, LATER)
    assert "start_without_terminal" in findings


def test_deadline_a_non_positive_bound_is_refused() -> None:
    from omnimarket.nodes.node_work_ledger_append_effect.adapters.run_deadline import (
        run_deadline,
    )

    with pytest.raises(ValueError, match="positive"), run_deadline(0):
        pass


def test_deadline_the_timer_is_disarmed_after_the_block() -> None:
    from omnimarket.nodes.node_work_ledger_append_effect.adapters.run_deadline import (
        run_deadline,
    )

    with run_deadline(1):
        pass
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_deadline_the_previous_sigalrm_handler_is_restored() -> None:
    from omnimarket.nodes.node_work_ledger_append_effect.adapters.run_deadline import (
        run_deadline,
    )

    previous = signal.getsignal(signal.SIGALRM)
    with run_deadline(1):
        pass
    assert signal.getsignal(signal.SIGALRM) is previous


def test_deadline_not_in_the_main_thread_raises_runtime_error() -> None:
    from omnimarket.nodes.node_work_ledger_append_effect.adapters.run_deadline import (
        run_deadline,
    )

    raised: list[BaseException] = []

    def _enter() -> None:
        try:
            with run_deadline(1):
                pass
        except BaseException as exc:
            raised.append(exc)

    worker = threading.Thread(target=_enter)
    worker.start()
    worker.join()

    assert len(raised) == 1
    assert isinstance(raised[0], RuntimeError)
    assert "main thread" in str(raised[0])


# --- the seam: the run's own limit sits inside the publish step's ---------------------------------------------------

_NODES = Path(__file__).resolve().parents[4] / "src/omnimarket/nodes"


def _publish_step_limit(limits: dict[str, int], step: str) -> int:
    return limits[step]


def _example_deadline() -> int:
    example = (
        _NODES / "node_work_ledger_append_effect/overlay_join_j5.example.yaml"
    ).read_text(encoding="utf-8")
    return ModelJoinJ5Overlay.model_validate(
        yaml.safe_load(example)
    ).run_deadline_seconds


def _publish_limits() -> dict[str, int]:
    return dict(TEST_LIMITS.step_timeout_seconds)


def test_seam_the_run_deadline_sits_inside_the_publish_steps_time_limit() -> None:
    limit = _publish_step_limit(_publish_limits(), "join_j5")
    assert _example_deadline() < limit


def test_control_a_publish_step_limit_at_or_below_the_deadline_fails_the_seam() -> None:
    limits = _publish_limits()
    limits["join_j5"] = _example_deadline()
    assert not _example_deadline() < _publish_step_limit(limits, "join_j5")


def test_seam_an_overlay_without_a_run_deadline_is_refused_naming_the_key() -> None:
    example = (
        _NODES / "node_work_ledger_append_effect/overlay_join_j5.example.yaml"
    ).read_text(encoding="utf-8")
    values = yaml.safe_load(example)
    del values["run_deadline_seconds"]
    with pytest.raises(ValidationError, match="run_deadline_seconds"):
        ModelJoinJ5Overlay.model_validate(values)
