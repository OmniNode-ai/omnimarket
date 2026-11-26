# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21288: a metering refresh costs O(new runs) and never holds the loop.

On the dev runtime every delegation terminal refolded all 139,891 stored runs of
the dashboard tenant inside the one runtime process (3 to 8 s CPU each, 57 in 40
minutes), and /health stalled with it. These tests pin the two properties the
fix owes, with no wall-clock assertion on the work itself:

* cost: the work of one refresh after one new run is the same whatever the
  tenant has stored (counted in records folded and records read, never seconds);
* loop: while a refresh over a large store runs, a coroutine on the same loop
  keeps ticking (its largest gap is bounded by a constant);

and the equality the incremental path owes the full rebuild: the rows an
incremental history leaves are the rows a rebuild on demand writes.

They run against an in-memory fake store and do not claim live Postgres
validation.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest

from omnimarket.adapters.asyncpg_adapter import _refuse_unbound_tenant_scoped_write
from omnimarket.models.model_metering_summary_refresh_requested import (
    EnumMeteringRefreshReason,
    ModelMeteringSummaryRefreshRequested,
)
from omnimarket.nodes.node_metering_summary_compute import (
    METERING_CAUSE_COLUMN,
    METERING_COLUMNS,
    HandlerMeteringSummary,
    ModelMeteringSummaryRequest,
)
from omnimarket.nodes.node_projection_metering_summary.handlers.handler_metering_summary_writer import (
    MeteringSummaryProjectionWriter,
)

pytestmark = pytest.mark.unit

_DEV = "820272f9-4aaf-5add-a2df-0af942852ab2"
_T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)

# The cost bound: a refresh after ONE new run folds this many records at most
# (the all window and the new run's day window each see that one record), and
# the work at 4N stored runs may exceed the work at N by at most the same
# constant. Neither is a duration.
_MAX_RECORDS_FOLDED_PER_NEW_RUN = 2
_MAX_RECORDS_READ_PER_NEW_RUN = 1
_MAX_GROWTH_WITH_STORED_RUNS = 0
_SMALL_STORE_RUNS = 400
_STORE_GROWTH_FACTOR = 4

# The loop bound: a coroutine that yields on every turn must see the loop come
# back within this many seconds while a refresh over _LARGE_STORE_RUNS runs.
# The synchronous fold of that fixture holds the loop for well over a second.
_MAX_LOOP_GAP_SECONDS = 0.5
_LARGE_STORE_RUNS = 40_000
_MIN_TICKS_DURING_REFRESH = 20


def _run(
    n: int,
    at: datetime,
    *,
    model: str = "qwen3.8-27b",
    tokens: int = 100,
    cause: str | None = None,
) -> dict[str, Any]:
    # The shape asyncpg returns: TIMESTAMPTZ as datetime, NUMERIC as Decimal. The
    # keys are the producer's own column list, so a column the reader adds or
    # drops breaks this fixture instead of leaving it green.
    row = dict(
        zip(
            METERING_COLUMNS,
            (
                f"run-{n:07d}",
                at,
                model,
                model,
                "document",
                tokens,
                tokens // 2,
                Decimal("0.0001"),
                Decimal("0.01"),
            ),
            strict=True,
        )
    )
    row[METERING_CAUSE_COLUMN] = cause
    return row


def _runs(count: int, *, start: int = 0, per_day: int = 40) -> list[dict[str, Any]]:
    models = ("qwen3.8-27b", "glm-5.3", "inkling-2-mini")
    return [
        _run(
            start + i,
            _T0 + timedelta(days=(start + i) // per_day, seconds=(start + i) % per_day),
            model=models[i % len(models)],
            tokens=0 if i % 17 == 0 else 100 + i % 50,
            cause="refused" if i % 23 == 0 else None,
        )
        for i in range(count)
    ]


class FakeStore:
    """Answers the refresh's reads by statement shape and keeps the upserted rows.

    The store is told apart by what a statement reads and with which parameters,
    never by an imported SQL constant, so the same fake drives the refresh before
    and after the change.
    """

    def __init__(self, runs: list[dict[str, Any]]) -> None:
        self.runs = runs
        self.rows: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.records_read = 0
        self.full_reads = 0
        self.incremental_reads = 0

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def execute(
        self, sql: str, *params: Any, tenant: str | None = None
    ) -> list[dict[str, Any]]:
        if "DISTINCT tenant_id" in sql:
            return [{"tenant_id": _DEV}]
        if "FROM public.delegation_events" in sql:
            assert tenant == params[0], "a tenant read runs under its own tenant"
            if len(params) == 1:
                self.full_reads += 1
                out = list(self.runs)
            else:
                # (tenant, watermark): the runs created at or after the watermark.
                self.incremental_reads += 1
                out = [r for r in self.runs if r["created_at"] >= params[1]]
            self.records_read += len(out)
            return out
        if "FROM public.metering_summary" in sql:
            tenant_id, baseline = str(params[0]), str(params[1])
            if len(params) == 2:
                row = self.rows.get((tenant_id, "all", "", baseline))
                return [] if row is None else [row]
            days = {str(d) for d in params[2]}
            return [
                row
                for (t, kind, start, base), row in sorted(self.rows.items())
                if t == tenant_id
                and base == baseline
                and kind == "day"
                and start in days
            ]
        if sql.startswith("INSERT INTO public.metering_summary"):
            _refuse_unbound_tenant_scoped_write(sql, tenant)
            columns = sql[sql.index("(") + 1 : sql.index(")")].split(", ")
            row = dict(zip(columns, params, strict=True))
            assert tenant == row["tenant_id"]
            key = (
                row["tenant_id"],
                row["window_kind"],
                row["window_start"],
                row["baseline_model"],
            )
            self.rows[key] = row
            return [{"tenant_id": row["tenant_id"]}]
        if "public.savings_priced_models" in sql:
            # OMN-26444: each refresh refills the priced models first, with no
            # tenant; the real adapter's refusal still applies.
            _refuse_unbound_tenant_scoped_write(sql, tenant)
            return []
        raise AssertionError(f"unexpected statement: {sql}")


def _writer(store: FakeStore) -> MeteringSummaryProjectionWriter:
    writer = MeteringSummaryProjectionWriter.__new__(MeteringSummaryProjectionWriter)
    writer._db = cast(Any, store)
    writer._standalone_bindings = None
    writer._coalescer = None
    writer._clock = None
    return writer


def _request(
    reason: EnumMeteringRefreshReason, days: list[date] | None = None, n: int = 0
) -> ModelMeteringSummaryRefreshRequested:
    return ModelMeteringSummaryRefreshRequested(
        refresh_id=f"omn21288-{reason.value}-{n}",
        reason=reason,
        tenant_ids=(_DEV,),
        days=None if days is None else frozenset(days),
        requested_at=datetime(2026, 11, 24, 19, 0, tzinfo=UTC),
    )


def _terminal(store: FakeStore, run: dict[str, Any], n: int) -> dict[str, Any]:
    store.runs.append(run)
    day = run["created_at"].date()
    return asyncio.run(
        _writer(store)._refresh(
            _request(EnumMeteringRefreshReason.DELEGATION_TERMINAL, [day], n)
        )
    )


def _bootstrap(store: FakeStore) -> None:
    """The first refresh of a tenant has nothing to continue from: it folds whole."""
    asyncio.run(_writer(store)._refresh(_request(EnumMeteringRefreshReason.SCHEDULE)))


@pytest.fixture(autouse=True)
def _no_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)


@pytest.fixture
def folded_records(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """The record count of every fold the canonical summary handler is asked for."""
    counts: list[int] = []
    real = HandlerMeteringSummary.handle

    def counting(
        self: HandlerMeteringSummary, payload: ModelMeteringSummaryRequest
    ) -> Any:
        counts.append(len(payload.records))
        return real(self, payload)

    monkeypatch.setattr(HandlerMeteringSummary, "handle", counting)
    return counts


def _work_of_one_terminal(stored: int, folded: list[int]) -> tuple[int, int]:
    store = FakeStore(_runs(stored))
    _bootstrap(store)
    folded.clear()
    store.records_read = 0
    newest = max(r["created_at"] for r in store.runs)
    result = _terminal(store, _run(stored, newest + timedelta(seconds=30)), n=stored)
    assert result["rows_upserted"] > 0, "the terminal's rows are stored"
    return sum(folded), store.records_read


def test_cost_of_a_terminal_refresh_does_not_grow_with_stored_runs(
    folded_records: list[int],
) -> None:
    small_folded, small_read = _work_of_one_terminal(_SMALL_STORE_RUNS, folded_records)
    large_folded, large_read = _work_of_one_terminal(
        _SMALL_STORE_RUNS * _STORE_GROWTH_FACTOR, folded_records
    )

    assert small_folded <= _MAX_RECORDS_FOLDED_PER_NEW_RUN
    assert large_folded <= _MAX_RECORDS_FOLDED_PER_NEW_RUN
    assert large_folded - small_folded <= _MAX_GROWTH_WITH_STORED_RUNS
    assert small_read <= _MAX_RECORDS_READ_PER_NEW_RUN
    assert large_read - small_read <= _MAX_GROWTH_WITH_STORED_RUNS


def test_a_terminal_refresh_never_reads_the_whole_store_once_state_exists() -> None:
    store = FakeStore(_runs(_SMALL_STORE_RUNS))
    _bootstrap(store)
    assert store.full_reads == 1, "the first refresh has no state: it folds whole"
    newest = max(r["created_at"] for r in store.runs)
    for i in range(3):
        _terminal(store, _run(9000 + i, newest + timedelta(minutes=i + 1)), n=i)
    assert store.full_reads == 1
    assert store.incremental_reads == 3


def _stored_rows(store: FakeStore) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    """Every stored row, with a day row's as_of left out.

    A day row's as_of is when that day was last folded; a rebuild folds every day
    at once and an incremental history folds a day only when a run lands in it,
    so the two differ there by design. Every other column, and the all row's
    as_of (the watermark), must be equal.
    """
    return {
        key: {c: v for c, v in row.items() if not (key[1] == "day" and c == "as_of")}
        for key, row in store.rows.items()
    }


def test_incremental_history_equals_a_full_rebuild_on_demand() -> None:
    first = _runs(300)
    store = FakeStore(list(first))
    _bootstrap(store)

    newest = max(r["created_at"] for r in store.runs)
    later = [
        _run(
            1000 + i,
            newest + timedelta(hours=i * 7, seconds=i),
            model=("glm-5.3", "inkling-2-mini", "qwen3.8-27b")[i % 3],
            tokens=0 if i % 5 == 0 else 80 + i,
            cause="timeout" if i % 4 == 0 else None,
        )
        for i in range(1, 40)
    ]
    for i, run in enumerate(later):
        _terminal(store, run, n=i)
    incremental = _stored_rows(store)
    assert store.full_reads == 1

    # The same runs, folded whole by a fresh store's first refresh.
    fresh = FakeStore(list(store.runs))
    _bootstrap(fresh)
    assert incremental == _stored_rows(fresh)

    # And the rebuild on demand, asked of the incremental store itself.
    asyncio.run(
        _writer(store)._refresh(_request(EnumMeteringRefreshReason.BACKFILL, n=99))
    )
    assert store.full_reads == 2
    assert incremental == _stored_rows(store)


def test_a_rebuild_on_demand_picks_up_a_run_older_than_the_watermark() -> None:
    store = FakeStore(_runs(120))
    _bootstrap(store)
    newest = max(r["created_at"] for r in store.runs)
    _terminal(store, _run(5000, newest + timedelta(minutes=5)), n=1)

    # A late run: created before the stored watermark, so the incremental path
    # (which continues from the watermark) does not see it; the rebuild does.
    store.runs.append(_run(5001, _T0 + timedelta(hours=1)))
    _terminal(store, _run(5002, newest + timedelta(minutes=9)), n=2)
    incremental_total = store.rows[(_DEV, "all", "", _baseline(store))]["runs_total"]
    assert incremental_total == len(store.runs) - 1

    asyncio.run(
        _writer(store)._refresh(_request(EnumMeteringRefreshReason.BACKFILL, n=3))
    )
    assert store.rows[(_DEV, "all", "", _baseline(store))]["runs_total"] == len(
        store.runs
    )


def _baseline(store: FakeStore) -> str:
    return next(key[3] for key in store.rows if key[1] == "all")


_GAP_ROUNDS = 3


async def _longest_gap(work: Awaitable[Any]) -> tuple[float, int, Any]:
    """The longest gap a yielding coroutine sees while ``work`` runs on the loop."""
    stop = asyncio.Event()
    gaps: list[float] = []

    async def ticker() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    tick = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    try:
        result = await work
    finally:
        stop.set()
        await tick
    return max(gaps), len(gaps), result


async def _quietest_gap(
    make_work: Callable[[], Awaitable[Any]], rounds: int = _GAP_ROUNDS
) -> tuple[float, int, Any]:
    """The smallest longest-gap over up to ``rounds`` runs of the same work.

    Code that holds the loop holds it the same way in every run, so its gap is the
    floor of every round. A stall of the runner (a noisy neighbour, a paused VM)
    lands in one round only, so the first clean round settles it. Hosted runs read
    0.60, 0.70 and 0.91 s for a refresh whose longest gap is 0.06 s on one lab CPU
    and 0.22 s with three busy loops sharing that CPU: the code holds nothing over
    100 ms, the runner stalled. Same method as omnibase_infra's loop-hold tests.
    """
    best: tuple[float, int, Any] | None = None
    for _ in range(rounds):
        measured = await _longest_gap(make_work())
        if best is None or measured[0] < best[0]:
            best = measured
        if measured[0] < _MAX_LOOP_GAP_SECONDS:
            break
    assert best is not None
    return best


def _large_refresh(*, inline_fold: bool = False) -> Callable[[], Awaitable[Any]]:
    async def work() -> Any:
        writer = _writer(FakeStore(_runs(_LARGE_STORE_RUNS, per_day=2000)))
        if inline_fold:

            async def on_the_loop(fn: Callable[..., Any], *args: Any) -> Any:
                return fn(*args)

            writer._run_blocking = on_the_loop
        return await writer._refresh(_request(EnumMeteringRefreshReason.SCHEDULE))

    return work


def test_the_refresh_does_not_hold_the_loop_over_a_large_store() -> None:
    # Steady state: the writer has already read its contract once (a one-time
    # parse that is not what a refresh costs), so warm it on a small store.
    _bootstrap(FakeStore(_runs(10)))
    max_gap, ticks, result = asyncio.run(_quietest_gap(_large_refresh()))
    assert result["rows_upserted"] > 0
    assert max_gap < _MAX_LOOP_GAP_SECONDS
    assert ticks >= _MIN_TICKS_DURING_REFRESH


def test_a_fold_made_synchronous_on_the_loop_is_read_as_a_hold() -> None:
    """Control: the same refresh with its fold run on the loop fails every round."""
    _bootstrap(FakeStore(_runs(10)))
    max_gap, _, result = asyncio.run(_quietest_gap(_large_refresh(inline_fold=True)))
    assert result["rows_upserted"] > 0
    assert max_gap >= _MAX_LOOP_GAP_SECONDS


def test_a_one_off_runner_stall_is_not_read_as_a_hold() -> None:
    """Control: one stalled round, the work itself holding nothing, passes."""
    rounds: list[int] = []

    async def work() -> str:
        rounds.append(1)
        if len(rounds) == 1:
            time.sleep(_MAX_LOOP_GAP_SECONDS * 1.5)  # the runner's stall
        await asyncio.sleep(0)
        return "done"

    max_gap, _, _ = asyncio.run(_quietest_gap(work))
    assert len(rounds) == 2
    assert max_gap < _MAX_LOOP_GAP_SECONDS
