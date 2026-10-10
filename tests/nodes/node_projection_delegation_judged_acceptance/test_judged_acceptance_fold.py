# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The judged acceptance fold: events in, cells out, a pure function of the set."""

from __future__ import annotations

import itertools
import random
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid5

import pytest

from omnimarket.events import ModelDelegationAcceptanceJudgedEvent
from omnimarket.models.delegation_acceptance_judge.enum_acceptance_failure_class import (
    EnumAcceptanceFailureClass,
)
from omnimarket.nodes.node_projection_delegation_judged_acceptance.handlers import (
    handler_projection_delegation_judged_acceptance as fold_module,
)
from omnimarket.nodes.node_projection_delegation_judged_acceptance.handlers.handler_projection_delegation_judged_acceptance import (
    CALIBRATION_MIN_AGREEMENT,
    CALIBRATION_MIN_KAPPA,
    MIN_CELL_VERDICTS,
    WINDOW_DAYS,
    HandlerProjectionDelegationJudgedAcceptance,
)
from omnimarket.nodes.node_projection_delegation_judged_acceptance.models import (
    ModelJudgedAcceptanceFoldRequest,
    ModelJudgedAcceptanceFoldResult,
)

TENANT = UUID("00000000-0000-4000-8000-000000000001")
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
FAILURE = next(iter(EnumAcceptanceFailureClass))


def _call_id(index: int) -> UUID:
    return uuid5(TENANT, f"call-{index}")


def _event(
    index: int,
    *,
    accept: bool = True,
    quality: int = 2,
    model: str = "qwen3.8-27b",
    tier: str = "local",
    task_type: str = "code_review",
    kind: str = "task",
    judge_run_id: str = "judge-run-a",
    judged_offset: int = 0,
    call_offset: int | None = None,
    agreement: float = 0.80,
    kappa: float = 0.65,
    rubric_version: str = "v1",
    tenant: UUID = TENANT,
) -> ModelDelegationAcceptanceJudgedEvent:
    return ModelDelegationAcceptanceJudgedEvent(
        correlation_id=_call_id(index),
        tenant_id=tenant,
        delegated_model_key=model,
        delegated_tier=tier,
        task_type=task_type,
        kind=kind,
        call_time=T0 + timedelta(minutes=index if call_offset is None else call_offset),
        judge_run_id=judge_run_id,
        judge_model="judge",
        judge_model_version="1",
        rubric_id="rubric",
        rubric_version=rubric_version,
        rubric_hash="hash",
        calibration_run_id="calibration-1",
        calibration_n=76,
        calibration_agreement=agreement,
        calibration_kappa=kappa,
        accept=accept,
        quality=quality,
        failure_class=FAILURE,
        judged_at=T0 + timedelta(days=1, seconds=judged_offset),
    )


def _fold(events: Sequence[ModelDelegationAcceptanceJudgedEvent]) -> Any:
    request = ModelJudgedAcceptanceFoldRequest(events=tuple(events))
    return HandlerProjectionDelegationJudgedAcceptance().handle(request)


def _rows(result: ModelJudgedAcceptanceFoldResult) -> list[dict[str, Any]]:
    return [cell.model_dump(mode="json") for cell in result.cells]


def _cell_events(
    accepts: int, rejects: int, **kw: Any
) -> list[ModelDelegationAcceptanceJudgedEvent]:
    start = kw.pop("start", 0)
    out = [_event(start + i, accept=True, **kw) for i in range(accepts)]
    out += [
        _event(start + accepts + i, accept=False, quality=0, **kw)
        for i in range(rejects)
    ]
    return out


def test_thresholds_are_the_plan_values() -> None:
    assert MIN_CELL_VERDICTS == 15
    assert CALIBRATION_MIN_AGREEMENT == 0.75
    assert CALIBRATION_MIN_KAPPA == 0.60
    assert WINDOW_DAYS == 30


def test_judged_acceptance_fold_cell_rate_is_accepts_over_judged_items() -> None:
    result = _fold(_cell_events(6, 12))
    (row,) = _rows(result)
    assert row["n"] == 18
    assert row["accepts"] == 6
    assert row["accept_rate"] == pytest.approx(6 / 18)
    # Wilson 95% for 6 of 18, computed by hand from the closed form.
    assert row["wilson_low"] == pytest.approx(0.1634, abs=1e-3)
    assert row["wilson_high"] == pytest.approx(0.5627, abs=1e-3)
    assert row["mean_quality"] == pytest.approx((6 * 2 + 12 * 0) / 18)
    assert row["judge_run_count"] == 1
    assert (row["tenant_id"], row["task_type"], row["kind"]) == (
        str(TENANT),
        "code_review",
        "task",
    )
    assert (row["delegated_tier"], row["delegated_model_key"]) == (
        "local",
        "qwen3.8-27b",
    )
    assert row["rubric_version"] == "v1"
    assert row["first_call_time"] == _event(0).call_time.isoformat().replace(
        "+00:00", "Z"
    )
    assert row["last_call_time"] == _event(17).call_time.isoformat().replace(
        "+00:00", "Z"
    )


def test_judged_acceptance_fold_terminal_ok_never_contributes() -> None:
    # Every delegated call reached terminal_ok, every verdict rejects.
    terminal_ok_by_call = {_call_id(i): True for i in range(20)}
    events = [_event(i, accept=False, quality=0) for i in range(20)]
    assert all(terminal_ok_by_call[e.correlation_id] for e in events)
    assert "terminal_ok" not in ModelDelegationAcceptanceJudgedEvent.model_fields
    assert "terminal_ok" not in ModelJudgedAcceptanceFoldRequest.model_fields
    (row,) = _rows(_fold(events))
    assert row["n"] == 20
    assert row["accepts"] == 0
    assert row["accept_rate"] == 0.0


def test_judged_acceptance_fold_wilson_bounds_for_all_accepted() -> None:
    (row,) = _rows(_fold(_cell_events(18, 0)))
    assert row["accept_rate"] == 1.0
    assert row["wilson_high"] == 1.0
    assert row["wilson_low"] == pytest.approx(0.8241, abs=1e-3)


def test_judged_acceptance_fold_cells_split_by_model_task_type_and_tier() -> None:
    events = (
        _cell_events(15, 0, model="m1")
        + _cell_events(15, 0, model="m2", start=100)
        + _cell_events(15, 0, task_type="test", start=200)
        + _cell_events(15, 0, tier="cloud", start=300)
    )
    assert len(_rows(_fold(events))) == 4


def test_underpowered_absent_cell_under_15_has_no_row() -> None:
    result = _fold(_cell_events(10, 4))
    assert result.cells == ()
    assert result.underpowered_cell_count == 1
    (row,) = _rows(_fold(_cell_events(10, 5)))
    assert row["n"] == 15


def test_underpowered_absent_counts_distinct_verdicts_not_deliveries() -> None:
    events = _cell_events(7, 7)
    assert _fold(events + events).cells == ()
    assert len(_fold(events + events + [_event(99)]).cells) == 1


def test_uncalibrated_judge_refused_below_agreement_or_kappa() -> None:
    low_agreement = _cell_events(15, 0, judge_run_id="bad-a", agreement=0.7499)
    low_kappa = _cell_events(15, 0, judge_run_id="bad-k", kappa=0.5999, start=100)
    result = _fold(low_agreement + low_kappa)
    assert result.cells == ()
    assert result.uncalibrated_refused_count == 30
    assert result.uncalibrated_judge_run_ids == ("bad-a", "bad-k")
    assert result.folded_verdict_count == 0


def test_uncalibrated_judge_refused_boundary_values_are_calibrated() -> None:
    events = _cell_events(15, 0, agreement=0.75, kappa=0.60)
    result = _fold(events)
    assert result.uncalibrated_refused_count == 0
    assert len(result.cells) == 1


def test_uncalibrated_judge_refused_never_displaces_a_calibrated_verdict() -> None:
    good = _cell_events(15, 0)
    # Later, rejecting, but from an uncalibrated run: refused, so it cannot win.
    bad = [
        _event(
            i,
            accept=False,
            quality=0,
            judge_run_id="bad",
            kappa=0.1,
            judged_offset=3600,
        )
        for i in range(15)
    ]
    result = _fold(good + bad)
    (row,) = _rows(result)
    assert row["accepts"] == 15
    assert result.uncalibrated_refused_count == 15


def test_probe_excluded_from_every_cell() -> None:
    real = _cell_events(15, 0)
    probes = [
        _event(500 + i).model_construct(
            **{**_event(500 + i).model_dump(), "kind": "probe"}
        )
        for i in range(10)
    ]
    assert {p.kind for p in probes} == {"probe"}
    result = _fold(real + probes)
    (row,) = _rows(result)
    assert row["n"] == 15
    assert result.excluded_kind_count == 10


def test_loop_turn_own_kind_never_pooled_with_task() -> None:
    events = _cell_events(15, 0, task_type="code_generation") + _cell_events(
        15, 0, task_type="code_generation", kind="edit_loop_turn", start=100
    )
    rows = _rows(_fold(events))
    assert sorted(r["kind"] for r in rows) == ["edit_loop_turn", "task"]
    assert all(r["n"] == 15 for r in rows)
    # Ten loop turns alone make no row, and do not top the task cell up.
    short = _cell_events(10, 0, task_type="code_generation") + _cell_events(
        10, 0, task_type="code_generation", kind="edit_loop_turn", start=100
    )
    assert _fold(short).cells == ()


def test_judged_acceptance_fold_latest_calibrated_verdict_wins_per_call() -> None:
    first = _cell_events(15, 0, judge_run_id="run-1")
    later_reject = [
        _event(i, accept=False, quality=0, judge_run_id="run-2", judged_offset=60)
        for i in range(15)
    ]
    (row,) = _rows(_fold(first + later_reject))
    assert row["n"] == 15
    assert row["accepts"] == 0
    assert row["judge_run_count"] == 1


def test_judged_acceptance_fold_tie_on_judged_at_goes_to_greater_judge_run_id() -> None:
    accepting = _cell_events(15, 0, judge_run_id="run-b")
    rejecting = [
        _event(i, accept=False, quality=0, judge_run_id="run-a") for i in range(15)
    ]
    for order in (accepting + rejecting, rejecting + accepting):
        (row,) = _rows(_fold(order))
        assert row["accepts"] == 15


def test_judged_acceptance_fold_window_is_thirty_days_before_newest_call() -> None:
    recent = _cell_events(15, 0, call_offset=0)
    # 31 days older than the newest call: outside the window.
    old = [_event(100 + i, call_offset=-31 * 24 * 60) for i in range(5)]
    # Exactly 30 days older: inside.
    edge = [_event(200 + i, call_offset=-30 * 24 * 60) for i in range(3)]
    (row,) = _rows(_fold(recent + old + edge))
    assert row["n"] == 18
    assert row["first_call_time"] == (T0 - timedelta(days=30)).isoformat().replace(
        "+00:00", "Z"
    )


def test_judged_acceptance_fold_window_never_reads_the_wall_clock() -> None:
    # A window anchored on today would drop events from 2001; the cell's own
    # newest call time anchors it, so a replay years later gives the same row.
    ancient = datetime(2001, 1, 1, tzinfo=UTC)
    events = [
        _event(i).model_copy(update={"call_time": ancient + timedelta(minutes=i)})
        for i in range(15)
    ]
    (row,) = _rows(_fold(events))
    assert row["n"] == 15
    assert _rows(_fold(events)) == _rows(_fold(list(reversed(events))))


def test_judged_acceptance_fold_tenants_do_not_mix() -> None:
    other = UUID("00000000-0000-4000-8000-000000000002")
    events = _cell_events(15, 0) + [
        _event(i, accept=False, tenant=other) for i in range(15)
    ]
    rows = _rows(_fold(events))
    assert {r["tenant_id"] for r in rows} == {str(TENANT), str(other)}


def test_judged_acceptance_fold_rubric_versions_make_separate_cells() -> None:
    events = _cell_events(15, 0) + _cell_events(15, 0, rubric_version="v2")
    assert sorted(r["rubric_version"] for r in _rows(_fold(events))) == ["v1", "v2"]


def test_judged_acceptance_fold_rows_come_out_in_cell_key_order() -> None:
    events = _cell_events(15, 0, model="zz") + _cell_events(
        15, 0, model="aa", start=100
    )
    keys = [r["delegated_model_key"] for r in _rows(_fold(events))]
    assert keys == sorted(keys)


# --- order independence and the arrival-order mutant ---------------------------


def _assert_order_independent(
    events: list[ModelDelegationAcceptanceJudgedEvent],
    fold: Callable[[Sequence[ModelDelegationAcceptanceJudgedEvent]], Any] = _fold,
    *,
    permutations: Sequence[Sequence[ModelDelegationAcceptanceJudgedEvent]],
) -> None:
    reference = fold(events).model_dump(mode="json")
    for permutation in permutations:
        assert fold(permutation).model_dump(mode="json") == reference
        # Redelivery of any one event changes nothing.
        for repeated in permutation:
            assert fold([*permutation, repeated]).model_dump(mode="json") == reference


def _conflicting_set() -> list[ModelDelegationAcceptanceJudgedEvent]:
    """Seven events: call 0 judged twice, 1 and 2 plus a refused run and a probe."""
    return [
        _event(0, accept=True, judge_run_id="run-1", judged_offset=0),
        _event(0, accept=False, quality=0, judge_run_id="run-2", judged_offset=10),
        _event(1, accept=True),
        _event(2, accept=False, quality=0),
        _event(3, accept=True, judge_run_id="run-1", judged_offset=5),
        _event(3, accept=False, quality=1, judge_run_id="run-3", judged_offset=5),
        _event(4, accept=True, judge_run_id="bad", kappa=0.2),
    ]


def test_order_independent_every_permutation_and_redelivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fold_module, "MIN_CELL_VERDICTS", 2)
    events = _conflicting_set()
    reference = _fold(events)
    assert reference.cells, "the fixed set must produce a row for the test to bite"
    permutations = list(itertools.permutations(events))
    assert len(permutations) == 5040
    _assert_order_independent(events, permutations=permutations[:: 5040 // 60])
    for permutation in permutations:
        assert _fold(permutation).model_dump(mode="json") == reference.model_dump(
            mode="json"
        )


def test_order_independent_full_size_cell_under_seeded_shuffles() -> None:
    events = [
        *_cell_events(9, 9),
        _event(2, accept=False, quality=0, judge_run_id="run-9", judged_offset=30),
        _event(20, accept=True, judge_run_id="bad", kappa=0.1),
        _event(21, accept=True, kind="edit_loop_turn"),
    ]
    rng = random.Random(20474)
    permutations = []
    for _ in range(40):
        shuffled = list(events)
        rng.shuffle(shuffled)
        permutations.append(shuffled)
    permutations.append(list(reversed(events)))
    _assert_order_independent(events, permutations=permutations)


def _arrival_order_winner(
    candidates: Sequence[ModelDelegationAcceptanceJudgedEvent],
) -> ModelDelegationAcceptanceJudgedEvent:
    return candidates[-1]


def test_mutant_arrival_order_fails_the_order_independence_test(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fold_module, "MIN_CELL_VERDICTS", 2)
    monkeypatch.setattr(fold_module, "_winning_verdict", _arrival_order_winner)
    events = _conflicting_set()
    with pytest.raises(AssertionError):
        _assert_order_independent(
            events, permutations=list(itertools.permutations(events))[:: 5040 // 60]
        )
