# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The MVP launch block's scheduled read goes to the source-control node only when its list for the schedule slot is not cached (OMN-19998).

Golden (the failing control of "a second cycle with unchanged sources reads from the cache"): the first refresh lists every producer
workflow; the second, past the refresh interval and in the same schedule slot, makes no run-list call and keeps the same runs with
this cycle's read time. A new slot, a run still in flight and a failed read are each read again. Error chain: a cache that cannot be
reached still returns the source's runs; with no cache every refresh past its interval reads every workflow. The named injected break
(a slot that changes on every call) turns the golden assertion red.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_floor_simple_effect.handlers import mvp_launch_reads
from omnimarket.nodes.node_floor_simple_effect.handlers.mvp_launch_reads import (
    declared_schedules,
    declared_sources,
    list_is_current,
    refresh,
)
from omnimarket.nodes.node_floor_simple_effect.runtime_floor_simple import (
    run_mvp_reads,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.cached_source_reader import (
    CachedSourceReader,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.handler_source_read_cache import (
    HandlerSourceReadCache,
)
from omnimarket.nodes.node_source_read_cache_effect.handlers.schedule_revision import (
    schedule_slot,
)
from tests.nodes.node_floor_simple_effect.mvp_support import MVP_NOW
from tests.nodes.node_floor_simple_effect.support import build_home
from tests.nodes.node_floor_simple_effect.test_mvp_launch_reads import (
    RT,
    overlay_model,
)
from tests.nodes.node_source_read_cache_effect.support import (
    PREFIX,
    TTL_BACKSTOP,
    DownBackend,
    memory_reader,
)

pytestmark = pytest.mark.unit

# Five minutes past the fixture overlay's :30 reference time: after every slot of its crons up to :34, and no slot is due in the next ten
# minutes, so the second cycle of a test sees the same slot as the first.
BASE = MVP_NOW + timedelta(minutes=5)
SOURCES = declared_sources(overlay_model())
SCHEDULES = declared_schedules(overlay_model())


def _slot(key: str, at: datetime) -> datetime:
    found = schedule_slot(SCHEDULES[key], at)
    assert found is not None
    return found


class Runner:
    """Stands in for ``onex scm run-list``: one run created a minute after the workflow's slot at the call's time; counts the calls."""

    def __init__(self, status: str = "completed", fail: bool = False) -> None:
        self.status = status
        self.fail = fail
        self.calls: list[tuple[str, str]] = []
        self.now = BASE

    def __call__(self, argv: Sequence[str], timeout: int) -> tuple[int, str, str]:
        workflow = argv[argv.index("--workflow") + 1]
        event = argv[argv.index("--event") + 1]
        self.calls.append((workflow, event))
        if self.fail:
            return 1, "", "boom"
        key = next(k for k, v in SOURCES.items() if v[1] == workflow)
        created = _slot(key, self.now) + timedelta(minutes=1)
        run = {
            "id": 1,
            "event": event,
            "status": self.status,
            "conclusion": "success" if self.status == "completed" else None,
            "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        return 0, json.dumps({"outcome": "completed", "result": {"runs": [run]}}), ""


def _refresh(
    home: Path, runner: Runner, at: datetime, cache: CachedSourceReader | None
) -> dict[str, object]:
    runner.now = at
    refresh(
        overlay_model(),
        home=home,
        onex_relpath="onex",
        reads_relpath=RT.reads_relpath,
        read_timeout_seconds=RT.read_timeout_seconds,
        stage_deadline_seconds=100_000,
        refresh_seconds=RT.refresh_seconds,
        run=runner,
        clock=lambda: at,
        cache=cache,
    )
    return json.loads((home / RT.reads_relpath).read_text(encoding="utf-8"))


def _past_interval(at: datetime) -> datetime:
    return at + timedelta(seconds=RT.refresh_seconds + 1)


def test_golden_a_second_cycle_in_the_same_slot_reads_from_the_cache(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    cache, _ = memory_reader()
    runner = Runner()
    first = _refresh(home, runner, BASE, cache)
    reads_first = len(runner.calls)
    assert reads_first == sum(len(v[3]) for v in SOURCES.values())
    second_at = _past_interval(BASE)
    second = _refresh(home, runner, second_at, cache)
    assert len(runner.calls) == reads_first, "the second cycle read a source again"
    assert [r["runs"] for r in second["reads"]] == [r["runs"] for r in first["reads"]]
    assert {r["read_at"] for r in second["reads"]} == {
        second_at.isoformat().replace("+00:00", "Z")
    } or all(datetime.fromisoformat(r["read_at"]) == second_at for r in second["reads"])
    assert cache.tally.hits == len(SOURCES)
    assert cache.tally.source_reads == len(SOURCES)


def test_a_new_schedule_slot_is_a_miss_and_is_read_again(tmp_path: Path) -> None:
    home = build_home(tmp_path)
    cache, _ = memory_reader()
    runner = Runner()
    _refresh(home, runner, BASE, cache)
    after = len(runner.calls)
    next_day = BASE + timedelta(days=8)
    _refresh(home, runner, next_day, cache)
    assert len(runner.calls) == after + after


def test_a_run_still_in_flight_is_read_again_every_cycle(tmp_path: Path) -> None:
    home = build_home(tmp_path)
    cache, backend = memory_reader()
    runner = Runner(status="in_progress")
    _refresh(home, runner, BASE, cache)
    after = len(runner.calls)
    _refresh(home, runner, _past_interval(BASE), cache)
    assert len(runner.calls) == 2 * after
    assert backend.keys == []


def test_error_chain_a_failed_read_is_never_cached(tmp_path: Path) -> None:
    home = build_home(tmp_path)
    cache, backend = memory_reader()
    failing = Runner(fail=True)
    _refresh(home, failing, BASE, cache)
    assert backend.keys == []
    healthy = Runner()
    _refresh(home, healthy, _past_interval(BASE), cache)
    assert len(healthy.calls) == sum(len(v[3]) for v in SOURCES.values())


def test_error_chain_an_unreachable_cache_still_returns_the_source_runs(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    cache = CachedSourceReader(
        HandlerSourceReadCache(DownBackend(), PREFIX, TTL_BACKSTOP)
    )
    runner = Runner()
    doc = _refresh(home, runner, BASE, cache)
    assert all(r["error"] is None and r["runs"] for r in doc["reads"])
    assert cache.tally.unavailable >= 1


def test_with_no_cache_every_cycle_past_the_interval_reads_every_workflow(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    runner = Runner()
    _refresh(home, runner, BASE, None)
    after = len(runner.calls)
    _refresh(home, runner, _past_interval(BASE), None)
    assert len(runner.calls) == 2 * after


def test_list_is_current_needs_the_slots_run_and_nothing_running() -> None:
    home_runner = Runner()
    key = next(iter(SOURCES))
    repo, workflow, limit, events = SOURCES[key]
    slot = _slot(key, BASE)
    read = mvp_launch_reads.read_workflow(
        Path("onex"), repo, workflow, limit, events, 1, home_runner, BASE
    )
    assert list_is_current(read, slot)
    assert not list_is_current(read, slot + timedelta(hours=1))
    running = Runner(status="in_progress")
    open_read = mvp_launch_reads.read_workflow(
        Path("onex"), repo, workflow, limit, events, 1, running, BASE
    )
    assert not list_is_current(open_read, slot)


def test_named_injected_break_a_slot_that_changes_every_call_reads_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = build_home(tmp_path)
    cache, _ = memory_reader()
    runner = Runner()
    _refresh(home, runner, BASE, cache)
    after = len(runner.calls)
    counter = {"n": 0}

    def drifting(crons: Sequence[str], now: datetime) -> datetime | None:
        counter["n"] += 1
        base = schedule_slot(crons, now)
        return base - timedelta(minutes=counter["n"]) if base else None

    monkeypatch.setattr(mvp_launch_reads, "schedule_slot", drifting)
    _refresh(home, runner, _past_interval(BASE), cache)
    assert len(runner.calls) > after


def test_the_publish_step_writes_the_cache_tally_receipt(tmp_path: Path) -> None:
    home = build_home(tmp_path)
    overlay = tmp_path / "mvp-overlay.yaml"
    import yaml

    from tests.nodes.node_floor_simple_effect.test_mvp_launch_reads import (
        ungraded_overlay,
    )

    overlay.write_text(yaml.safe_dump(ungraded_overlay()), encoding="utf-8")
    cache_overlay = tmp_path / "cache-overlay.yaml"
    cache_overlay.write_text(
        yaml.safe_dump(
            {
                "key_prefix": "t:",
                "ttl_backstop_seconds": 60,
                "clone_relpath_template": "{name}",
                "clone_revision_timeout_seconds": 5,
            }
        ),
        encoding="utf-8",
    )
    env = {
        "ONEX_MVP_LAUNCH_OVERLAY": str(overlay),
        "ONEX_SOURCE_READ_CACHE_OVERLAY": str(cache_overlay),
    }
    assert run_mvp_reads(str(home), env, Runner(), lambda: BASE) == 0
    receipt = json.loads(
        (home / ".onex_state/source-cache/mvp_launch_reads.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["backend"] == "memory"
    assert receipt["source_reads"] == len(SOURCES)
    assert receipt["disabled_reason"] == ""


def test_the_publish_step_with_no_cache_overlay_reads_and_says_why(
    tmp_path: Path,
) -> None:
    import yaml

    from tests.nodes.node_floor_simple_effect.test_mvp_launch_reads import (
        ungraded_overlay,
    )

    home = build_home(tmp_path)
    overlay = tmp_path / "mvp-overlay.yaml"
    overlay.write_text(yaml.safe_dump(ungraded_overlay()), encoding="utf-8")
    env = {
        "ONEX_MVP_LAUNCH_OVERLAY": str(overlay),
        "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path),
    }
    runner = Runner()
    assert run_mvp_reads(str(home), env, runner, lambda: BASE) == 0
    assert runner.calls
    receipt = json.loads(
        (home / ".onex_state/source-cache/mvp_launch_reads.json").read_text(
            encoding="utf-8"
        )
    )
    assert receipt["disabled_reason"].startswith("overlay: ")


MISSING = "GitHub GET /repos/x/actions/workflows/y/runs answered HTTP 404: Not Found"


class MissingRunner(Runner):
    """Answers HTTP 404 for the workflows named (they do not exist on GitHub), as the source-control node does."""

    def __init__(self, missing: set[str]) -> None:
        super().__init__()
        self.missing = missing

    def __call__(self, argv: Sequence[str], timeout: int) -> tuple[int, str, str]:
        workflow = argv[argv.index("--workflow") + 1]
        if workflow in self.missing:
            self.calls.append((workflow, argv[argv.index("--event") + 1]))
            return (
                0,
                json.dumps(
                    {
                        "outcome": "failed",
                        "result": {"failure_reason": MISSING},
                    }
                ),
                "",
            )
        return super().__call__(argv, timeout)


class Tip:
    """Stands in for the clone's tip read: one commit per repository that the test can move."""

    def __init__(self) -> None:
        self.sha = "a" * 40
        self.reads = 0

    def __call__(self, argv: Sequence[str], timeout: int) -> tuple[int, str, str]:
        self.reads += 1
        return 0, self.sha + "\n", ""


def _clones(home: Path) -> None:
    for repo, *_rest in SOURCES.values():
        (home / repo.rsplit("/", 1)[-1]).mkdir(exist_ok=True)


def _refresh_with_tip(
    home: Path,
    runner: Runner,
    at: datetime,
    cache: CachedSourceReader,
    tip: Tip,
) -> None:
    runner.now = at
    refresh(
        overlay_model(),
        home=home,
        onex_relpath="onex",
        reads_relpath=RT.reads_relpath,
        read_timeout_seconds=RT.read_timeout_seconds,
        stage_deadline_seconds=100_000,
        refresh_seconds=RT.refresh_seconds,
        run=runner,
        clock=lambda: at,
        cache=cache,
        revision_run=tip,
    )


def test_a_workflow_github_does_not_have_is_not_asked_again_until_the_default_branch_moves(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    _clones(home)
    cache, _ = memory_reader()
    absent = sorted({v[1] for v in SOURCES.values()})[:3]
    runner = MissingRunner(set(absent))
    tip = Tip()
    _refresh_with_tip(home, runner, BASE, cache, tip)
    first_missing_reads = sum(1 for call in runner.calls if call[0] in absent)
    assert first_missing_reads >= len(absent)
    _refresh_with_tip(home, runner, _past_interval(BASE), cache, tip)
    second_missing_reads = sum(1 for call in runner.calls if call[0] in absent)
    assert second_missing_reads == first_missing_reads, (
        "a missing workflow was asked about again"
    )
    tip.sha = "b" * 40
    _refresh_with_tip(home, runner, _past_interval(_past_interval(BASE)), cache, tip)
    third_missing_reads = sum(1 for call in runner.calls if call[0] in absent)
    assert third_missing_reads > second_missing_reads, (
        "a moved default branch was not a miss"
    )


def test_a_clone_that_cannot_be_read_leaves_the_missing_workflow_read_every_cycle(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    cache, _ = memory_reader()
    absent = sorted({v[1] for v in SOURCES.values()})[:1]
    runner = MissingRunner(set(absent))
    tip = Tip()
    _refresh_with_tip(home, runner, BASE, cache, tip)
    _refresh_with_tip(home, runner, _past_interval(BASE), cache, tip)
    assert sum(1 for call in runner.calls if call[0] in absent) >= 2 * len(absent)
    assert tip.reads == 0


def test_an_error_that_is_not_a_missing_workflow_is_never_remembered(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    _clones(home)
    cache, backend = memory_reader()
    runner = Runner(fail=True)
    _refresh_with_tip(home, runner, BASE, cache, Tip())
    assert backend.keys == []
