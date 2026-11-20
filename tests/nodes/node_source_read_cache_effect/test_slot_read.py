# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The shared slot read of the board's steps: an unfinished slot is served pending and re-checked once per contract interval (OMN-26241).

Golden (``pending``): the slot's run is unfinished; the first cycle reads the source, a second cycle inside the interval serves that read
marked pending since the slot with its own read time and does not read the source, and a cycle after the interval reads it again. Error
chains: a read that failed is never kept pending; a finished run is a hit by its slot key. ``shared_helper``: both steps read through
the helper and keep no slot logic of their own. ``missing_lookup_counted_separately``: a missing workflow's lookup is not a miss.
``recorded_tally``: the tally lines recorded on 2026-11-20 are reproduced by the slot-keyed read the code had, and the same cycles read
through the helper cost under 10 seconds of source on the second cycle.
"""

from __future__ import annotations

import inspect
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from omnimarket.models.model_source_read_cache import (
    ModelSourceCacheKey,
    ModelSourceCacheTally,
)
from omnimarket.nodes.node_beta_board_effect.handlers import adapter_beta_board_sources
from omnimarket.nodes.node_floor_simple_effect.handlers import mvp_launch_reads
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader import (
    CachedSourceReader,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.schedule_revision import (
    SLOT_STAMP,
    schedule_slot,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.slot_read import (
    SlotReadPlan,
    pending_recheck_interval,
    read_slot_through_cache,
)
from tests.nodes.node_source_read_cache_effect.support import Clock, memory_reader

pytestmark = pytest.mark.unit

HOURLY = "0 * * * *"
# Five minutes into the 18:00 slot of an hourly cron.
AT = datetime(2026, 11, 20, 18, 5, tzinfo=UTC)
SLOT = datetime(2026, 11, 20, 18, 0, tzinfo=UTC)
MISSING = "HTTP 404"
FIXTURE = (
    Path(__file__).parent / "fixtures" / "factory_floor_publish_tally_2026-11-20.txt"
)


class Read(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    read_at: datetime
    finished: bool = False
    error: str = ""
    pending_since: datetime | None = None


class Source:
    """A source that counts its reads, each taking ``seconds`` on the cache's clock."""

    def __init__(self, clock: Clock | None = None, seconds: float = 0.0) -> None:
        self.reads = 0
        self.finished = False
        self.error = ""
        self.at = AT
        self._clock = clock
        self._seconds = seconds

    def fetch(self, name: str = "probe") -> Read:
        self.reads += 1
        if self._clock is not None:
            self._clock.now += self._seconds
        return Read(
            name=name, read_at=self.at, finished=self.finished, error=self.error
        )


def _plan(
    source: Source,
    *,
    name: str = "probe",
    head: str | None = None,
    crons: tuple[str, ...] = (HOURLY,),
) -> SlotReadPlan[Read]:
    return SlotReadPlan[Read](
        source_id=f"test-probe:{name}",
        params={"limit": 5},
        crons=crons,
        absent_source_id=f"test-probe-absent:{name}",
        workflow=name,
        head=head,
        fetch=lambda: source.fetch(name),
        encode=lambda read: read.model_dump_json(),
        decode=Read.model_validate_json,
        is_current=lambda read, slot: read.finished and read.read_at >= slot,
        is_readable=lambda read: not read.error,
        is_missing=lambda read: MISSING in read.error,
        with_read_at=lambda read, at: read.model_copy(update={"read_at": at}),
        with_pending=lambda read, slot: read.model_copy(update={"pending_since": slot}),
    )


def test_pending_an_unfinished_slot_is_served_from_the_cache_inside_the_interval() -> (
    None
):
    cache, _ = memory_reader()
    source = Source()
    first = read_slot_through_cache(cache, _plan(source), AT)
    inside = AT + pending_recheck_interval() - timedelta(minutes=1)
    second = read_slot_through_cache(cache, _plan(source), inside)
    assert source.reads == 1, "two cycles inside the interval read the source twice"
    assert first.pending_since == SLOT
    assert second.pending_since == SLOT
    assert second.read_at == AT, "the pending read must carry its own read time"
    assert cache.tally.pending_served == 1
    assert cache.tally.misses == 1


def test_pending_the_source_is_read_again_once_the_interval_has_passed() -> None:
    cache, _ = memory_reader()
    source = Source()
    read_slot_through_cache(cache, _plan(source), AT)
    after = AT + pending_recheck_interval() + timedelta(seconds=1)
    source.at = after
    read_slot_through_cache(cache, _plan(source), after)
    assert source.reads == 2
    # The interval counts from the last check: the next cycle inside it reads nothing.
    read_slot_through_cache(cache, _plan(source), after + timedelta(minutes=5))
    assert source.reads == 2


def test_pending_the_interval_is_the_contracts_and_is_fifteen_minutes() -> None:
    assert pending_recheck_interval() == timedelta(minutes=15)


def test_pending_a_finished_run_is_stored_under_its_slot_and_hit_afterwards() -> None:
    cache, backend = memory_reader()
    source = Source()
    source.finished = True
    first = read_slot_through_cache(cache, _plan(source), AT)
    second = read_slot_through_cache(cache, _plan(source), AT + timedelta(minutes=5))
    assert source.reads == 1
    assert first.pending_since is None
    assert second.pending_since is None
    assert second.read_at == AT + timedelta(minutes=5)
    assert any(SLOT.strftime(SLOT_STAMP) in key for key in backend.keys)


def test_pending_a_failed_read_is_not_kept_and_the_next_cycle_reads_again() -> None:
    cache, backend = memory_reader()
    source = Source()
    source.error = "exit 1: boom"
    read_slot_through_cache(cache, _plan(source), AT)
    read_slot_through_cache(cache, _plan(source), AT + timedelta(minutes=1))
    assert source.reads == 2
    assert backend.keys == []


def test_pending_a_pending_read_from_an_older_slot_is_not_served_past_a_later_check() -> (
    None
):
    cache, _ = memory_reader()
    source = Source()
    read_slot_through_cache(cache, _plan(source), AT)
    # An hour later (next slot), outside the interval: read again, pending since the new slot.
    later = AT + timedelta(hours=1)
    source.at = later
    read = read_slot_through_cache(cache, _plan(source), later)
    assert source.reads == 2
    assert read.pending_since == SLOT + timedelta(hours=1)


def test_shared_helper_both_steps_read_through_it_and_keep_no_slot_logic() -> None:
    for module in (adapter_beta_board_sources, mvp_launch_reads):
        text = inspect.getsource(module)
        assert "read_slot_through_cache" in text, module.__name__
        for own_logic in ("schedule_slot", "store_if=", "SLOT_STAMP"):
            assert own_logic not in text, f"{module.__name__} keeps {own_logic}"


def test_shared_helper_is_the_call_each_step_makes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests.nodes.node_beta_board_effect.test_probe_read_cache import (
        NOW,
        Runner,
        _refresh,
    )
    from tests.nodes.node_floor_simple_effect.support import build_home
    from tests.nodes.node_floor_simple_effect.test_mvp_launch_reads_cache import (
        BASE,
    )
    from tests.nodes.node_floor_simple_effect.test_mvp_launch_reads_cache import (
        Runner as MvpRunner,
    )
    from tests.nodes.node_floor_simple_effect.test_mvp_launch_reads_cache import (
        _refresh as mvp_refresh,
    )

    calls: list[str] = []
    for module in (adapter_beta_board_sources, mvp_launch_reads):
        real = module.read_slot_through_cache

        def spy(
            cache: CachedSourceReader,
            plan: SlotReadPlan[object],
            now: datetime,
            recheck: timedelta | None = None,
            real: object = real,
            label: str = module.__name__,
        ) -> object:
            calls.append(label)
            return real(cache, plan, now, recheck)  # type: ignore[operator]

        monkeypatch.setattr(module, "read_slot_through_cache", spy)
    beta_cache, _ = memory_reader()
    _refresh(tmp_path, Runner(), NOW + timedelta(minutes=2), beta_cache)
    mvp_cache, _ = memory_reader()
    mvp_refresh(build_home(tmp_path / "mvp"), MvpRunner(), BASE, mvp_cache)
    assert adapter_beta_board_sources.__name__ in calls
    assert mvp_launch_reads.__name__ in calls


def test_missing_lookup_counted_separately_does_not_raise_misses() -> None:
    cache, _ = memory_reader()
    source = Source()
    source.error = f"answered {MISSING}: Not Found"
    plan = _plan(source, head="c0ffee")
    read_slot_through_cache(cache, plan, AT)
    assert cache.tally.absent_lookups == 1
    assert cache.tally.absent_hits == 0
    # One source read, one miss: the absent lookup added none.
    assert cache.tally.source_reads == 1
    assert cache.tally.misses == 1
    again = read_slot_through_cache(cache, plan, AT + timedelta(minutes=5))
    assert source.reads == 1
    assert again.read_at == AT + timedelta(minutes=5)
    assert cache.tally.absent_hits == 1
    assert cache.tally.misses == 1
    assert cache.tally.hits == 0


def test_missing_lookup_counted_separately_at_the_reader() -> None:
    cache, _ = memory_reader()
    key = ModelSourceCacheKey(
        source_id="absent",
        params_digest=ModelSourceCacheKey.digest({"w": 1}),
        revision="head",
    )
    assert cache.lookup(key, Read.model_validate_json, absent=True) is None
    assert cache.tally.misses == 0
    assert cache.tally.absent_lookups == 1


def _recorded() -> dict[str, ModelSourceCacheTally]:
    found: dict[str, ModelSourceCacheTally] = {}
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        if not line.startswith("source-cache:"):
            continue
        fields = dict(re.findall(r"(\w+)=(\S+)", line))
        found[fields["step"]] = ModelSourceCacheTally(
            hits=int(fields["hits"]),
            misses=int(fields["misses"]),
            stores=int(fields["stores"]),
            source_reads=int(fields["source_reads"]),
            source_seconds=float(fields["source_seconds"]),
            backend=fields["backend"],
        )
    return found


def _old_slot_read(
    cache: CachedSourceReader, source: Source, name: str, at: datetime
) -> Read:
    """The read the steps made before the fix: keyed by slot, stored only when the slot's run had finished, nothing kept otherwise."""
    slot = schedule_slot([HOURLY], at)
    assert slot is not None
    read, _ = cache.read(
        ModelSourceCacheKey(
            source_id=f"old:{name}",
            params_digest=ModelSourceCacheKey.digest({"limit": 5}),
            revision=slot.strftime(SLOT_STAMP),
        ),
        lambda: source.fetch(name),
        lambda value: value.model_dump_json(),
        Read.model_validate_json,
        usable=lambda value: value.finished,
        store_if=lambda value: value.finished,
    )
    return read


def test_recorded_tally_the_old_read_reproduces_the_recorded_cost() -> None:
    recorded = _recorded()
    for step in ("beta_board", "mvp_launch_reads"):
        tally = recorded[step]
        clock = Clock(0.0)
        cache, _ = memory_reader(clock)
        source = Source(clock, tally.source_seconds / tally.source_reads)
        for cycle in range(2):
            for index in range(tally.source_reads):
                _old_slot_read(
                    cache, source, f"{step}-{index}", AT + timedelta(minutes=5 * cycle)
                )
        # Two cycles of the recorded reads: every read goes to the source again and nothing is stored.
        assert cache.tally.stores == 0
        assert cache.tally.source_reads == 2 * tally.source_reads


def test_recorded_tally_the_same_cycles_through_the_helper_cost_under_ten_seconds() -> (
    None
):
    recorded = _recorded()
    assert recorded["factory_floor"].hits == 15
    for step in ("beta_board", "mvp_launch_reads"):
        tally = recorded[step]
        assert tally.stores == 0
        assert tally.source_seconds >= 50
        clock = Clock(0.0)
        cache, _ = memory_reader(clock)
        source = Source(clock, tally.source_seconds / tally.source_reads)
        plans = [
            _plan(source, name=f"{step}-{index}") for index in range(tally.source_reads)
        ]
        for plan in plans:
            read_slot_through_cache(cache, plan, AT)
        assert cache.tally.source_seconds == tally.source_seconds
        before = cache.tally.source_seconds
        for plan in plans:
            read_slot_through_cache(cache, plan, AT + timedelta(minutes=5))
        second_cycle_seconds = cache.tally.source_seconds - before
        assert second_cycle_seconds < 10, (
            f"{step}: the second cycle read the source for {second_cycle_seconds} s"
        )
        assert cache.tally.stores > 0
        assert cache.tally.pending_served == tally.source_reads
