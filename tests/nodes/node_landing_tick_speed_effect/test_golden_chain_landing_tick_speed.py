# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cache judging-path observations and ledger indexes without controller scripts."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omnimarket.nodes.node_landing_tick_speed_effect import NodeLandingTickSpeedEffect
from omnimarket.nodes.node_landing_tick_speed_effect import models as speed_models
from omnimarket.nodes.node_landing_tick_speed_effect.handlers import (
    handler_landing_tick_speed as speed,
)

pytestmark = pytest.mark.unit
SHA = {c: c * 40 for c in "abcdef"}
X = "example#1"
NOW = "2030-01-01T08:00:00Z"
lf = SimpleNamespace(
    is_cause_key=lambda key: key.startswith("cause:"),
    short_key=lambda key: key,
    repo_of=lambda _key: "example/repo",
    LANDING_BRANCH="main",
    retired_judge_disarm_of=lambda old: old.get("disarm_event"),
    judge_paths_text=str,
)


def _judge_snap(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        live={X: SimpleNamespace(base="dev", state="OPEN", head=SHA["e"])},
        default_branches={},
        state_dir=tmp_path,
        timing={},
    )


def test_a_pair_read_clean_is_not_diffed_again_on_the_next_tick(tmp_path: Path) -> None:
    seen: list[tuple[Any, ...]] = []

    def read(*args: Any) -> dict[str, Any]:
        seen.append(args)
        return {"status": "clean", "paths": [], "detail": ""}

    leases = [{"lease_id": 1, "pr": X, "dispatch_head": SHA["a"]}]
    found = [{"lease_id": 1, "head_sha": SHA["e"]}]
    for _tick in range(3):
        snap = _judge_snap(tmp_path)
        touches, events = speed.judge_touches(
            lf, snap, leases, found, {}, NOW, read, tmp_path, snap.timing
        )
        assert touches == {}
        assert events == []
    assert seen == [("example/repo", SHA["a"], SHA["e"], "dev", "1")]
    assert snap.timing["judge_touch_reads"] == 0
    assert snap.timing["judge_touch_cached"] == 1
    assert "judge_touches_s" in snap.timing


def test_a_touched_pair_is_served_from_the_cache_and_still_recorded(
    tmp_path: Path,
) -> None:
    calls = []

    def read(*args: Any) -> dict[str, Any]:
        calls.append(args)
        return {
            "status": "touched",
            "paths": [{"path": ".github/workflows/ci.yml", "kind": "workflow"}],
            "detail": "",
        }

    leases = [{"lease_id": 1, "pr": X, "dispatch_head": SHA["a"]}]
    found = [{"lease_id": 1, "head_sha": SHA["e"]}]
    first, events = speed.judge_touches(
        lf, _judge_snap(tmp_path), leases, found, {}, NOW, read, tmp_path
    )
    assert first[X]["status"] == "touched"
    assert len(events) == 1
    again, events = speed.judge_touches(
        lf, _judge_snap(tmp_path), leases, found, {}, NOW, read, tmp_path
    )
    assert again[X]["paths"] == first[X]["paths"]
    assert len(events) == 1
    assert len(calls) == 1


def test_an_unreadable_pair_is_read_again_and_the_cache_keeps_only_current_pairs(
    tmp_path: Path,
) -> None:
    answers = iter(
        [
            {
                "status": "unreadable",
                "paths": [{"path": "(unreadable diff)", "kind": "unreadable"}],
                "detail": "commit eeeeeeeeeeee is not in example/repo",
            },
            {"status": "clean", "paths": [], "detail": ""},
        ]
    )
    calls = []

    def read(*args: Any) -> dict[str, Any]:
        calls.append(args)
        return next(answers)

    leases = [
        {"lease_id": 1, "pr": X, "dispatch_head": SHA["a"]},
        {"lease_id": 2, "pr": X, "dispatch_head": SHA["b"]},
    ]
    touches, _ = speed.judge_touches(
        lf,
        _judge_snap(tmp_path),
        leases,
        [{"lease_id": 1, "head_sha": SHA["e"]}],
        {},
        NOW,
        read,
        tmp_path,
    )
    assert touches[X]["status"] == "unreadable"
    assert not (tmp_path / speed.JUDGE_CACHE_FILE).exists()
    speed.judge_touches(
        lf,
        _judge_snap(tmp_path),
        leases,
        [{"lease_id": 2, "head_sha": SHA["e"]}],
        {},
        NOW,
        read,
        tmp_path,
    )
    assert len(calls) == 2
    kept = json.loads((tmp_path / speed.JUDGE_CACHE_FILE).read_text())
    assert list(kept) == [f"example/repo:dev:{SHA['b']}..{SHA['e']}"]


def test_the_node_entrypoint_routes_the_judge_read(tmp_path: Path) -> None:
    req = speed_models.ModelLandingTickSpeedRequest(
        operation="judge_touches",
        state_dir=tmp_path,
        lf=lf,
        snap=_judge_snap(tmp_path),
        leases=[{"lease_id": 1, "pr": X, "dispatch_head": SHA["a"]}],
        results=[{"lease_id": 1, "head_sha": SHA["e"]}],
        prior={},
        now=NOW,
        read_touch=lambda *_args: {"status": "clean", "paths": [], "detail": ""},
    )
    assert NodeLandingTickSpeedEffect().handle(req).value == ({}, [])


def cache_entry(path: Path) -> tuple[tuple[str, str], dict[str, Any]]:
    return (str(path), "rows"), {
        "stat": (path.stat().st_size, path.stat().st_mtime_ns),
        "entries": [("stamp", "CLAIM", "lane-a")],
        "digest": b"digest",
        "tail": (0, 0, b""),
    }


def test_ledger_index_loads_before_read_and_saves_changes(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text("claim\n")
    key, entry = cache_entry(ledger)
    cache = {key: entry}
    state = tmp_path / "state"
    timing: dict[str, Any] = {}
    first = speed.ledger_indexed(
        cache, state, lambda: list(cache[key]["entries"]), timing
    )
    assert first == entry["entries"]
    assert timing["ledger_index_written"] == 1
    assert timing["ledger_index_files"] == 0
    with ledger.open("a") as stream:
        stream.write("terminal\n")
    cache = {}
    timing = {}

    def read() -> list[Any]:
        assert cache[key] == entry
        cache[key]["stat"] = (ledger.stat().st_size, ledger.stat().st_mtime_ns)
        cache[key]["entries"].append(("stamp", "TERMINAL", "lane-a"))
        return list(cache[key]["entries"])

    second = speed.ledger_indexed(cache, state, read, timing)
    assert [row[1] for row in second] == ["CLAIM", "TERMINAL"]
    assert timing["ledger_index_files"] == 1
    assert timing["ledger_index_written"] == 1
    cache = {}
    timing = {}
    assert (
        speed.ledger_indexed(cache, state, lambda: cache[key]["entries"], timing)
        == second
    )
    assert timing["ledger_index_written"] == 0
    assert "ledger_index_load_s" in timing
    assert "ledger_index_save_s" in timing


def test_index_roundtrip_and_in_process_cache_wins(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text("claim\n")
    key, entry = cache_entry(ledger)
    directory = tmp_path / "index"
    assert speed.save_ledger_index({key: entry}, directory, {}) == 1
    loaded: dict[Any, Any] = {}
    stamps = speed.load_ledger_index(loaded, directory)
    assert loaded == {key: entry}
    assert stamps == {key: entry["stat"]}
    assert speed.save_ledger_index(loaded, directory, stamps) == 0
    newer = {**entry, "entries": ["in-process"]}
    assert speed.load_ledger_index({key: newer}, directory) == {}
    ledger.unlink()
    assert speed.load_ledger_index({}, directory) == {}
    assert not list(directory.glob("*.marshal"))


def test_unreadable_and_stale_indexes_are_dropped(tmp_path: Path) -> None:
    directory = tmp_path / "index"
    directory.mkdir()
    junk = directory / "junk.marshal"
    junk.write_bytes(b"not marshal")
    assert speed.load_ledger_index({}, directory) == {}
    assert not junk.exists()
    ledger = tmp_path / "ledger.md"
    ledger.write_text("claim\n")
    key, entry = cache_entry(ledger)
    speed.save_ledger_index({key: entry}, directory, {})
    old = time.time() - speed.LEDGER_INDEX_KEEP_S - 60
    os.utime(ledger, (old, old))
    assert speed.load_ledger_index({}, directory) == {}
    assert not list(directory.glob("*.marshal"))


def test_ledger_without_cache_or_directory_calls_reader(tmp_path: Path) -> None:
    calls = []
    without_cache = speed.ledger_indexed(
        None, tmp_path, lambda: calls.append(1) or "answer"
    )
    assert without_cache == "answer"
    without_directory = speed.ledger_indexed(
        {}, None, lambda: calls.append(2) or "answer"
    )
    assert without_directory == "answer"
    assert calls == [1, 2]
    assert not list(tmp_path.iterdir())
    req = speed_models.ModelLandingTickSpeedRequest(
        operation="ledger_indexed",
        rows_cache={},
        state_dir=tmp_path,
        read=lambda: "answer",
    )
    assert NodeLandingTickSpeedEffect().handle(req).value == "answer"


@pytest.mark.parametrize("operation", ["judge_touches", "ledger_indexed"])
def test_handler_requires_operation_reader(operation: str) -> None:
    with pytest.raises(ValueError, match="needs read"):
        NodeLandingTickSpeedEffect().handle(
            speed_models.ModelLandingTickSpeedRequest(operation=operation)
        )


def test_handler_shares_caller_ledger_cache_and_timing(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.md"
    ledger.write_text("claim\n")
    key, entry = cache_entry(ledger)
    directory = tmp_path / speed.LEDGER_INDEX_DIR
    speed.save_ledger_index({key: entry}, directory, {})
    cache: dict[Any, Any] = {}
    timing: dict[str, Any] = {}

    def read() -> list[Any]:
        assert cache[key] == entry
        cache[key]["entries"].append(("stamp", "MSG", "lane-a"))
        cache[key]["stat"] = ("changed",)
        return cache[key]["entries"]

    request = speed_models.ModelLandingTickSpeedRequest(
        operation="ledger_indexed",
        state_dir=tmp_path,
        rows_cache=cache,
        timing=timing,
        read=read,
    )
    result = NodeLandingTickSpeedEffect().handle(request)
    assert result.value == cache[key]["entries"]
    assert timing["ledger_index_files"] == 1
    assert timing["ledger_index_written"] == 1
    reloaded: dict[Any, Any] = {}
    speed.load_ledger_index(reloaded, directory)
    assert reloaded == cache
