# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23018 AC1: the history is backfilled from the dated sources that already hold it, and a day with no source reading stays unread."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_floor_compare_effect.handlers.adapter_floor_history import (
    read_history,
    reading_at,
)
from omnimarket.nodes.node_floor_compare_effect.handlers.adapter_floor_sources import (
    SRC_FRICTION,
    backfill_snapshots,
    friction_at,
    load_book,
    load_friction_rules,
)
from omnimarket.nodes.node_floor_compare_effect.handlers.handler_floor_compare import (
    HandlerFloorCompare,
    build_comparison,
)
from omnimarket.nodes.node_floor_compare_effect.models.model_floor_compare import (
    ModelFloorCompareRequest,
    ModelFloorComparison,
)
from tests.nodes.node_floor_compare_effect.support import (
    NOW,
    RT,
    build_home,
    snap,
)

pytestmark = pytest.mark.unit

# Small thresholds so a few planted rows are a source that recorded; the contract's own are far larger.
HS = RT.history.model_copy(
    update={
        "min_alive": {"ledger": 5, "landing-decisions": 3, "lab-alarm": 2},
        "min_tick_samples": 2,
    }
)
DAY = timedelta(days=1)


def stamp(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def row(at: datetime, kind: str, *cells: str) -> str:
    return " | ".join([stamp(at), kind, *cells]) + "\n"


def ledger_file(tmp: Path, rows: list[str]) -> Path:
    path = tmp / "ledger" / "ROLLING_WORK_LEDGER.md"
    path.parent.mkdir(parents=True)
    path.write_text("".join(rows), encoding="utf-8")
    return path


def heartbeat(start: datetime, hours: int) -> list[str]:
    """STATUS rows every 15 minutes: the ledger was written to, so an empty count is a real zero."""
    return [
        row(start + timedelta(minutes=15 * n), "STATUS", "lane=tick")
        for n in range(hours * 4)
    ]


def planted_day(at: datetime, *, done: int, partial: int, claims: int) -> list[str]:
    rows = [row(at + timedelta(minutes=n), "CLAIM", "lane=x") for n in range(claims)]
    rows += [
        row(at + timedelta(minutes=30 + n), "TERMINAL", "lane=x", "outcome=done")
        for n in range(done)
    ]
    rows += [
        row(at + timedelta(minutes=60 + n), "TERMINAL", "lane=x", "outcome=partial")
        for n in range(partial)
    ]
    return rows


def points(book, end: datetime, days: int = 4):
    return backfill_snapshots(book, HS, end, days, 1500, load_friction_rules(HS))


def test_a_planted_ledger_day_yields_the_right_point(tmp_path: Path) -> None:
    day = datetime(2026, 11, 5, 6, 0, tzinfo=UTC)
    rows = heartbeat(day - timedelta(hours=12), 36) + planted_day(
        day, done=2, partial=1, claims=3
    )
    path = ledger_file(tmp_path, rows)
    book = load_book(
        HS, {"ONEX_LEDGER_PATH": str(path)}, tmp_path, NOW - 6 * DAY, ("ledger",)
    )
    ledger = [p for p in points(book, NOW) if p.source == "backfill:ledger"]
    held = next(p for p in ledger if p.at == datetime(2026, 11, 5, 18, tzinfo=UTC))
    assert held.values["lanes_started"] == 3.0
    assert held.values["lanes_ended:done"] == 2.0
    assert held.values["lanes_ended:partial"] == 1.0
    assert held.values["lanes_ended:failed"] == 0.0
    assert all(p.source == "backfill:ledger" for p in ledger)


def test_failing_control_a_day_the_ledger_did_not_record_stays_unread_never_zero(
    tmp_path: Path,
) -> None:
    quiet_from = datetime(2026, 11, 5, 0, 0, tzinfo=UTC)
    rows = heartbeat(quiet_from - 2 * DAY, 48) + planted_day(
        quiet_from - DAY, done=2, partial=0, claims=3
    )
    path = ledger_file(tmp_path, rows)
    book = load_book(
        HS, {"ONEX_LEDGER_PATH": str(path)}, tmp_path, NOW - 6 * DAY, ("ledger",)
    )
    ledger = [p for p in points(book, NOW) if p.source == "backfill:ledger"]
    stored = {p.at for p in ledger}
    assert datetime(2026, 11, 4, 12, tzinfo=UTC) in stored
    assert (
        datetime(2026, 11, 6, 12, tzinfo=UTC) not in stored
    )  # nothing written for a whole day
    reading = reading_at(tuple(ledger), "lanes_started", NOW - DAY, 3)
    assert reading.read is False
    assert reading.value is None


def test_the_cycle_backfills_the_ledger_and_the_one_day_view_shows_better_and_worse(
    tmp_path: Path,
) -> None:
    yesterday = NOW - DAY - timedelta(hours=12)
    today = NOW - timedelta(hours=12)
    rows = heartbeat(NOW - 3 * DAY, 72)
    rows += planted_day(yesterday, done=2, partial=1, claims=3)
    rows += planted_day(today, done=5, partial=4, claims=3)
    path = ledger_file(tmp_path, rows)
    home = build_home(tmp_path)
    sink = InMemoryEmissionSink()
    request = ModelFloorCompareRequest(
        home=str(home), now=NOW, backfill_history=True, history_days=3
    )
    result = HandlerFloorCompare(sink=sink, clock=lambda: NOW).run(
        request, environ={"ONEX_LEDGER_PATH": str(path), "OMNI_HOME": str(home)}
    )
    assert result.ok
    assert result.history_written["backfill:ledger"] > 24
    doc = ModelFloorComparison.model_validate_json(
        (home / RT.output_relpath).read_text()
    )
    day = doc.windows[0]
    better = {ln.key: ln for ln in day.better}
    worse = {ln.key: ln for ln in day.worse}
    assert (better["lanes_ended:done"].old, better["lanes_ended:done"].new) == (
        2.0,
        5.0,
    )
    assert (worse["lanes_ended:partial"].old, worse["lanes_ended:partial"].new) == (
        1.0,
        4.0,
    )
    assert "Lower is better, so this is worse." in worse["lanes_ended:partial"].words
    assert doc.windows[0].counts["better"] >= 1
    again = HandlerFloorCompare(sink=InMemoryEmissionSink(), clock=lambda: NOW).run(
        request, environ={"ONEX_LEDGER_PATH": str(path), "OMNI_HOME": str(home)}
    )
    assert (
        again.history_written == {}
    )  # a stored point (same source, same time) is never written twice
    assert len(
        {(s.source, s.at) for s in read_history(home / RT.history_relpath)[0]}
    ) == len(read_history(home / RT.history_relpath)[0])


def test_landing_decisions_count_merged_and_refused_by_the_window(
    tmp_path: Path,
) -> None:
    base = NOW - timedelta(hours=80)
    lines = [
        {
            "kind": "release",
            "outcome": "released",
            "ts": stamp(base + timedelta(hours=n)),
        }
        for n in range(80)
    ]
    lines += [
        {
            "kind": "merge",
            "outcome": "merged",
            "ts": stamp(NOW - timedelta(hours=30, minutes=n)),
        }
        for n in range(2)
    ]
    lines += [
        {
            "kind": "merge",
            "outcome": "merged",
            "ts": stamp(NOW - timedelta(hours=2, minutes=n)),
        }
        for n in range(5)
    ]
    lines += [
        {
            "kind": "contract_heal",
            "outcome": "refused",
            "ts": stamp(NOW - timedelta(hours=3)),
        }
    ]
    directory = tmp_path / "landing"
    directory.mkdir()
    (directory / "decisions.jsonl").write_text(
        "".join(json.dumps(item) + "\n" for item in lines), encoding="utf-8"
    )
    book = load_book(
        HS,
        {"LANDING_STATE_DIR": str(directory)},
        tmp_path,
        NOW - 5 * DAY,
        ("landing-decisions",),
    )
    landing = [
        p for p in points(book, NOW, 2) if p.source == "backfill:landing-decisions"
    ]
    last = max(landing, key=lambda p: p.at)
    assert last.values["landing_merged"] == 5.0
    assert last.values["landing_refused"] == 1.0
    older = reading_at(tuple(landing), "landing_merged", NOW - timedelta(hours=20), 3)
    assert older.value == 2.0


def test_a_missing_landing_directory_is_a_note_and_no_point(tmp_path: Path) -> None:
    book = load_book(HS, {}, tmp_path, NOW - DAY, ("landing-decisions",))
    assert book.notes
    assert "LANDING_STATE_DIR is not set" in book.notes[0]
    assert [p for p in points(book, NOW) if "landing" in p.source] == []


def test_the_tick_log_gives_the_goal_levels_and_parked_causes_dated_by_the_watcher_tick(
    tmp_path: Path,
) -> None:
    log = tmp_path / HS.throughput_relpath
    log.parent.mkdir(parents=True)
    block = (
        "NOTE controller live: tick running\n"
        "NOTE merges last 60 min: {m} (open 90, watcher tick {t})\n"
        "NOTE lab-fill | sent={s} (claude=1)\n"
        "NOTE escalated:x | parked cause cause:a until 2026-11-09T00:00:00Z\n"
        "NOTE escalated:y | parked cause cause:b until 2026-11-09T00:00:00Z\n"
        "GOAL changed=no | merges_60m={g} open={o} green_waiting=0\n"
    )
    text = ""
    for n, (m, s, g, o) in enumerate([(7, 3, 2, 9), (8, 5, 1, 8)]):
        text += block.format(
            m=m, s=s, g=g, o=o, t=stamp(NOW - timedelta(hours=3 - 2 * n))
        )
    text += "NOTE controller live: tick running\nNOTE merges last 60 min: 1 (open 1, watcher tick not-a-time)\n"
    log.write_text(text, encoding="utf-8")
    book = load_book(HS, {}, tmp_path, NOW - DAY, ("throughput-tick",))
    tick = [p for p in points(book, NOW, 1) if p.source == "backfill:throughput-tick"]
    level = next(
        p for p in tick if "merges_all" in p.values and p.values["merges_all"] == 7.0
    )
    assert level.values == {
        "merges_all": 7.0,
        "parked_causes": 2.0,
        "merges": 2.0,
        "open_prs": 9.0,
    }
    assert level.at == NOW - timedelta(hours=3)
    mean = [p for p in tick if "lab_fill_sent_per_tick" in p.values]
    assert mean
    assert mean[-1].values["lab_fill_sent_per_tick"] == 4.0
    assert not any(
        p.values.get("merges_all") == 1.0 for p in tick
    )  # an unreadable tick time is not a point


def test_friction_points_list_every_class_so_a_class_that_stopped_reads_zero_and_is_better(
    tmp_path: Path,
) -> None:
    start = NOW - 3 * DAY
    rows = heartbeat(start, 72)
    rows += [
        row(
            NOW - timedelta(days=1, hours=14, minutes=n),
            "FRICTION",
            "lane=a",
            "class=disk-full",
            "cost: 10 minutes",
        )
        for n in range(3)
    ]
    rows += [
        row(
            NOW - timedelta(hours=5, minutes=n),
            "FRICTION",
            "lane=a",
            "class=hook-refusal-new",
            "cost: 4 minutes",
        )
        for n in range(2)
    ]
    path = ledger_file(tmp_path, rows)
    book = load_book(
        HS, {"ONEX_LEDGER_PATH": str(path)}, tmp_path, NOW - 5 * DAY, ("ledger",)
    )
    old_at = (NOW - timedelta(days=1, hours=2)).replace(
        minute=7, second=0, microsecond=0
    )
    values, labels = friction_at(book, HS, old_at, load_friction_rules(HS))
    assert values["cause:stoppage:disk-full"] == 3.0
    assert values["stoppage_cost:stoppage:disk-full"] == 30.0
    assert values["friction_rows"] == 3.0
    assert labels["cause:stoppage:disk-full"] == "disk full"
    friction = [p for p in points(book, NOW) if p.source == f"backfill:{SRC_FRICTION}"]
    assert friction
    assert all(
        p.complete == ("cause:stoppage:", "stoppage_cost:stoppage:") for p in friction
    )
    now = snap(0, {"cause:stoppage:other": 1.0}).model_copy(
        update={"complete": friction[0].complete}
    )
    window = build_comparison(tuple(friction), now, RT).windows[0]
    better = {ln.key: ln for ln in window.better}
    assert (
        better["cause:stoppage:disk-full"].old,
        better["cause:stoppage:disk-full"].new,
    ) == (3.0, 0.0)
    unlisted = build_comparison(
        tuple(friction), snap(0, {"cause:stoppage:other": 1.0}), RT
    ).windows[0]
    assert "cause:stoppage:disk-full" in {
        ln.key for ln in unlisted.unread
    }  # a tick that lists no classes proves no zero


def test_git_merges_are_counted_per_repository_from_the_clone_history(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    repo, stray = home / "alpha", home / "stray"
    repo.mkdir(parents=True)
    stray.mkdir(parents=True)
    identity = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }

    def git(where: Path, *args: str, when: datetime | None = None) -> None:
        dated = (
            {
                "GIT_COMMITTER_DATE": when.isoformat(),
                "GIT_AUTHOR_DATE": when.isoformat(),
            }
            if when is not None
            else {}
        )
        subprocess.run(
            ["git", "-C", str(where), *args],
            check=True,
            capture_output=True,
            env={**scrub_git_location_env(os.environ), **identity, **dated},
        )

    git(repo, "init", "-q", "-b", "main")
    git(stray, "init", "-q", "-b", "main")  # a directory with .git and no origin/HEAD
    for n, hours in enumerate([40, 30, 2, 1]):
        git(
            repo,
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            f"merge {n}",
            when=NOW - timedelta(hours=hours),
        )
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    book = load_book(HS, {}, home, NOW - 4 * DAY, ("git",))
    git_points = [
        p
        for p in backfill_snapshots(book, HS, NOW, 2, 1500, load_friction_rules(HS))
        if p.source == "backfill:git"
    ]
    last = max(git_points, key=lambda p: p.at)
    assert last.values["git_merges:alpha"] == 2.0
    assert last.values["git_merges_total"] == 2.0
    assert "git_merges:stray" not in last.values
    assert any("stray" in note for note in book.notes)
