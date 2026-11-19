# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24492: the metering refresh coalescer.

At most one day-refresh per tenant per interval; the days held back inside
the interval join the next run, which carries them, and ``complete`` settles
them. A whole refresh always runs and settles every tenant's pending days.
The clock is injected; the overlay supplies the interval, and a broken
overlay is refused, never read as an absent one.
"""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime, timedelta

import pytest

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.models.model_metering_summary_refresh_requested import (
    EnumMeteringRefreshReason,
    ModelMeteringSummaryRefreshRequested,
)
from omnimarket.nodes.node_projection_metering_summary.metering_refresh_coalescer import (
    MeteringRefreshCoalescer,
    coalesce_interval_from_overlay,
)
from omnimarket.nodes.node_projection_metering_summary.models.model_metering_refresh_coalesce import (
    EnumMeteringRefreshVerdict,
    ModelMeteringRefreshCoalesceDecision,
)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 11, 15, 0, 0, tzinfo=UTC)
D1 = date(2026, 11, 15)
D2 = date(2026, 11, 16)
D3 = date(2026, 11, 17)
D4 = date(2026, 11, 18)
INTERVAL = timedelta(seconds=300)


def terminal_request(
    refresh_id: str,
    requested_at: datetime,
    day: date = D1,
    tenant_ids: tuple[str, ...] = ("t1",),
) -> ModelMeteringSummaryRefreshRequested:
    """One delegation terminal's refresh: one tenant, one day."""
    return ModelMeteringSummaryRefreshRequested(
        refresh_id=refresh_id,
        reason=EnumMeteringRefreshReason.DELEGATION_TERMINAL,
        tenant_ids=tenant_ids,
        days=frozenset({day}),
        requested_at=requested_at,
    )


def whole_request(
    refresh_id: str, requested_at: datetime
) -> ModelMeteringSummaryRefreshRequested:
    """One schedule window's refresh: every tenant, every day."""
    return ModelMeteringSummaryRefreshRequested(
        refresh_id=refresh_id,
        reason=EnumMeteringRefreshReason.SCHEDULE,
        tenant_ids=None,
        days=None,
        requested_at=requested_at,
    )


def test_golden_first_terminal_runs_and_the_next_inside_the_interval_is_held_back() -> (
    None
):
    coalescer = MeteringRefreshCoalescer(INTERVAL)
    first = terminal_request("r1", T0)
    assert coalescer.admit(first, T0).verdict is EnumMeteringRefreshVerdict.RUN
    second = terminal_request("r2", T0 + timedelta(seconds=10))
    held = coalescer.admit(second, T0 + timedelta(seconds=10))
    assert held.verdict is EnumMeteringRefreshVerdict.COALESCED
    assert held.request is None
    stats = coalescer.stats()
    assert (stats.runs, stats.coalesced) == (1, 1)
    assert stats.pending == {"t1": frozenset({D1})}


def test_golden_a_terminal_after_the_interval_runs_with_the_held_back_days() -> None:
    coalescer = MeteringRefreshCoalescer(INTERVAL)
    assert (
        coalescer.admit(terminal_request("r1", T0, D1), T0).verdict
        is EnumMeteringRefreshVerdict.RUN
    )
    assert (
        coalescer.admit(
            terminal_request("r2", T0 + timedelta(seconds=10), D2),
            T0 + timedelta(seconds=10),
        ).verdict
        is EnumMeteringRefreshVerdict.COALESCED
    )
    third = coalescer.admit(
        terminal_request("r3", T0 + timedelta(seconds=310), D3),
        T0 + timedelta(seconds=310),
    )
    assert third.verdict is EnumMeteringRefreshVerdict.RUN
    assert third.request is not None
    assert third.request.tenant_ids == ("t1",)
    assert third.request.days == frozenset({D2, D3})
    assert third.settles == (("t1", frozenset({D2})),)
    coalescer.complete(third)
    assert coalescer.stats().pending == {}


def test_golden_a_whole_refresh_always_runs_and_holds_back_terminals_just_after_it() -> (
    None
):
    coalescer = MeteringRefreshCoalescer(INTERVAL)
    whole1 = coalescer.admit(whole_request("w1", T0), T0)
    assert whole1.verdict is EnumMeteringRefreshVerdict.RUN
    assert whole1.request is not None
    assert whole1.request.tenant_ids is None
    assert whole1.tenants == ()
    assert (
        coalescer.admit(
            terminal_request("r1", T0 + timedelta(seconds=10)),
            T0 + timedelta(seconds=10),
        ).verdict
        is EnumMeteringRefreshVerdict.COALESCED
    )
    whole2 = coalescer.admit(
        whole_request("w2", T0 + timedelta(seconds=310)),
        T0 + timedelta(seconds=310),
    )
    assert whole2.verdict is EnumMeteringRefreshVerdict.RUN
    assert whole2.settles == (("t1", frozenset({D1})),)
    coalescer.complete(whole2)
    assert coalescer.stats().pending == {}


def test_golden_zero_interval_runs_every_request_unchanged() -> None:
    coalescer = MeteringRefreshCoalescer(timedelta(0))
    for i in range(2):
        at = T0 + timedelta(seconds=i)
        request = terminal_request(f"r{i}", at)
        decision = coalescer.admit(request, at)
        assert decision.verdict is EnumMeteringRefreshVerdict.RUN
        assert decision.request == request
        assert decision.settles == ()
        assert decision.tenants == ()
    assert (coalescer.stats().runs, coalescer.stats().coalesced) == (2, 0)


def test_golden_two_tenants_are_held_back_independently() -> None:
    coalescer = MeteringRefreshCoalescer(INTERVAL)
    assert coalescer.admit(terminal_request("a1", T0, D1, ("t1",)), T0).verdict is (
        EnumMeteringRefreshVerdict.RUN
    )
    assert (
        coalescer.admit(
            terminal_request("b1", T0 + timedelta(seconds=10), D2, ("t2",)),
            T0 + timedelta(seconds=10),
        ).verdict
        is EnumMeteringRefreshVerdict.RUN
    )
    assert (
        coalescer.admit(
            terminal_request("a2", T0 + timedelta(seconds=20), D2, ("t1",)),
            T0 + timedelta(seconds=20),
        ).verdict
        is EnumMeteringRefreshVerdict.COALESCED
    )
    assert (
        coalescer.admit(
            terminal_request("b2", T0 + timedelta(seconds=310), D3, ("t2",)),
            T0 + timedelta(seconds=310),
        ).verdict
        is EnumMeteringRefreshVerdict.RUN
    )
    stats = coalescer.stats()
    assert (stats.runs, stats.coalesced) == (3, 1)
    assert stats.pending == {"t1": frozenset({D2})}


def test_error_rollback_lets_the_retry_run_at_once_and_keeps_pending() -> None:
    coalescer = MeteringRefreshCoalescer(INTERVAL)
    first = coalescer.admit(terminal_request("r1", T0, D1), T0)
    assert first.verdict is EnumMeteringRefreshVerdict.RUN
    assert (
        coalescer.admit(
            terminal_request("r2", T0 + timedelta(seconds=10), D2),
            T0 + timedelta(seconds=10),
        ).verdict
        is EnumMeteringRefreshVerdict.COALESCED
    )
    coalescer.rollback(first)
    retry = coalescer.admit(
        terminal_request("r3", T0 + timedelta(seconds=20), D3),
        T0 + timedelta(seconds=20),
    )
    assert retry.verdict is EnumMeteringRefreshVerdict.RUN
    stats = coalescer.stats()
    assert (stats.runs, stats.coalesced) == (1, 1)
    assert stats.pending == {"t1": frozenset({D2})}


def test_error_negative_elapsed_is_due_not_held_back() -> None:
    coalescer = MeteringRefreshCoalescer(INTERVAL)
    assert coalescer.admit(terminal_request("r1", T0), T0).verdict is (
        EnumMeteringRefreshVerdict.RUN
    )
    assert (
        coalescer.admit(
            terminal_request("r2", T0 - timedelta(seconds=10), D2),
            T0 - timedelta(seconds=10),
        ).verdict
        is EnumMeteringRefreshVerdict.RUN
    )


def test_error_a_whole_refresh_that_names_no_tenant_changes_nothing_for_named_runs_set_before() -> (
    None
):
    coalescer = MeteringRefreshCoalescer(INTERVAL)
    assert coalescer.admit(terminal_request("r1", T0), T0).verdict is (
        EnumMeteringRefreshVerdict.RUN
    )
    assert (
        coalescer.admit(
            whole_request("w1", T0 + timedelta(seconds=100)),
            T0 + timedelta(seconds=100),
        ).verdict
        is EnumMeteringRefreshVerdict.RUN
    )
    assert (
        coalescer.admit(
            terminal_request("r2", T0 + timedelta(seconds=50), D2),
            T0 + timedelta(seconds=50),
        ).verdict
        is EnumMeteringRefreshVerdict.RUN
    )


def test_error_overlay_absent_returns_default_interval() -> None:
    assert coalesce_interval_from_overlay(120, environ={}) == timedelta(seconds=120)


def test_error_overlay_with_unknown_field_is_refused(tmp_path) -> None:
    overlay = tmp_path / "node_projection_metering_summary" / "overlay.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("min_interval_seconds: 60\nbogus_field: 1\n", encoding="utf-8")
    with pytest.raises(NodeOverlayError) as excinfo:
        coalesce_interval_from_overlay(
            60, environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
        )
    assert not isinstance(excinfo.value, NodeOverlayNotFoundError)


def test_error_overlay_negative_interval_is_refused(tmp_path) -> None:
    overlay = tmp_path / "node_projection_metering_summary" / "overlay.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("min_interval_seconds: -1\n", encoding="utf-8")
    with pytest.raises(NodeOverlayError) as excinfo:
        coalesce_interval_from_overlay(
            60, environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
        )
    assert not isinstance(excinfo.value, NodeOverlayNotFoundError)


def test_golden_overlay_value_is_read(tmp_path) -> None:
    overlay = tmp_path / "node_projection_metering_summary" / "overlay.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("min_interval_seconds: 45\n", encoding="utf-8")
    assert coalesce_interval_from_overlay(
        60, environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}
    ) == timedelta(seconds=45)


def test_control_without_coalescing_the_burst_runs_every_time() -> None:
    plain = MeteringRefreshCoalescer(timedelta(0))
    for i in range(10):
        at = T0 + timedelta(seconds=i)
        assert plain.admit(terminal_request(f"r{i}", at), at).verdict is (
            EnumMeteringRefreshVerdict.RUN
        )
    stats = plain.stats()
    assert stats.runs == 10
    assert stats.coalesced == 0

    gated = MeteringRefreshCoalescer(INTERVAL)
    for i in range(10):
        at = T0 + timedelta(seconds=i)
        verdict = gated.admit(terminal_request(f"r{i}", at), at).verdict
        assert verdict is (
            EnumMeteringRefreshVerdict.RUN
            if i == 0
            else EnumMeteringRefreshVerdict.COALESCED
        )
    stats = gated.stats()
    assert stats.runs == 1
    assert stats.coalesced == 9


def test_golden_stats_count_every_held_back_request() -> None:
    coalescer = MeteringRefreshCoalescer(INTERVAL)
    assert coalescer.admit(terminal_request("r0", T0, D1), T0).verdict is (
        EnumMeteringRefreshVerdict.RUN
    )
    for i, day in enumerate((D2, D3, D4)):
        at = T0 + timedelta(seconds=10 + 10 * i)
        assert (
            coalescer.admit(terminal_request(f"r{i + 1}", at, day), at).verdict
            is EnumMeteringRefreshVerdict.COALESCED
        )
    stats = coalescer.stats()
    assert stats.runs == 1
    assert stats.coalesced == 3
    assert stats.pending == {"t1": frozenset({D2, D3, D4})}


def test_control_threads_never_admit_two_runs_inside_one_interval() -> None:
    coalescer = MeteringRefreshCoalescer(INTERVAL)
    barrier = threading.Barrier(8)
    results: list[ModelMeteringRefreshCoalesceDecision] = []
    results_lock = threading.Lock()

    def worker(index: int) -> None:
        barrier.wait()
        decision = coalescer.admit(terminal_request(f"r{index}", T0), T0)
        with results_lock:
            results.append(decision)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 8
    assert (
        sum(
            1
            for decision in results
            if decision.verdict is EnumMeteringRefreshVerdict.RUN
        )
        == 1
    )
