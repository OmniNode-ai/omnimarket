# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity of the facts read with the retired script on 20 recorded ticks (OMN-23734, AC2).

``fixtures/recorded_facts_ticks.json`` holds 20 ticks recorded by running the script's own readers
(``read_launchctl``, ``read_controller_files``, ``read_watcher_and_floors``, ``read_open_history``,
``read_repo_status_units`` and the goal check's readers) against real files and scripted launchctl and
ps programs: each tick's files and the facts the script read. The test rebuilds the files and programs
under a temporary directory and reads them with the host source, so the same files give the same facts.
"""

from __future__ import annotations

import json
import shutil
import stat
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_throughput_tick_facts_effect.handlers.handler_throughput_tick_facts import (
    HandlerThroughputTickFacts,
)
from omnimarket.nodes.node_throughput_tick_facts_effect.handlers.tick_facts_ports import (
    ScriptedTickFactsSource,
)
from omnimarket.nodes.node_throughput_tick_facts_effect.models.model_throughput_tick_facts import (
    ModelProcessOutcome,
    ModelThroughputTickFactsRequest,
)

_RAW = (Path(__file__).parent / "fixtures" / "recorded_facts_ticks.json").read_text(
    encoding="utf-8"
)
_N = len(json.loads(_RAW)["ticks"])


@pytest.fixture
def short_root() -> Iterator[Path]:
    """A short root: the script cuts an error text at 120 characters, so the recorded texts hold only under a root as short as the recording's."""
    root = Path(tempfile.mkdtemp(prefix="tf", dir="/tmp"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _tick(index: int, root: Path) -> dict[str, Any]:
    ticks: list[dict[str, Any]] = json.loads(
        _RAW.replace("{ROOT}", json.dumps(str(root))[1:-1])
    )["ticks"]
    return ticks[index]


def _program(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _build(tick: dict[str, Any], root: Path) -> ModelThroughputTickFactsRequest:
    for rel in tick["dirs"]:
        (root / rel).mkdir(parents=True, exist_ok=True)
    for rel, text in tick["files"].items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    for name in ("landing", "state", "bin", "roots"):
        (root / name).mkdir(exist_ok=True)
    spec = tick["spec"]["launchctl"]
    if spec["mode"] != "missing":
        out = spec["stdout"]
        _program(
            root / "bin" / "launchctl",
            f"cat <<'EOF'\n{out}EOF\nexit 0\n"
            if spec["mode"] == "loaded"
            else "exit 1\n",
        )
    if spec["mode"] == "loaded" and spec["ps"] == "ok":
        _program(root / "bin" / "ps", f"echo '{spec['etime']}'\n")
    elif spec["mode"] == "loaded" and spec["ps"] == "fail":
        _program(root / "bin" / "ps", "exit 1\n")
    given = tick["request"]
    return ModelThroughputTickFactsRequest(
        now=given["now"],
        launchctl=str(root / "bin" / "launchctl"),
        ps=str(root / "bin" / "ps"),
        landing_state_dir=str(root / "landing"),
        watcher_state_path=str(root / "watcher" / "state.json")
        if given["watcher_given"]
        else None,
        floors_path=str(root / "floors.json"),
        state_dir=str(root / "state"),
        open_basis=given["open_basis"],
        overlay_roots=str(root / "roots"),
    )


@pytest.mark.parametrize("index", range(_N))
def test_facts_equal_what_the_script_read_parity(index: int, short_root: Path) -> None:
    tick = _tick(index, short_root)
    result = HandlerThroughputTickFacts().handle(_build(tick, short_root))
    assert result.facts.model_dump(exclude_unset=True) == tick["expected"]["facts"]
    expected_goal = tick["expected"]["goal"]
    goal = result.goal.model_dump(mode="json")
    if goal["scope"] is not None:
        goal["scope"] = {
            k: sorted(v) if isinstance(v, list) else v for k, v in goal["scope"].items()
        }
    assert {k: v for k, v in goal.items() if k in expected_goal} == expected_goal


def test_the_recording_holds_twenty_ticks_and_every_branch() -> None:
    ticks = json.loads(_RAW)["ticks"]
    assert len(ticks) == 20
    facts = [t["expected"]["facts"] for t in ticks]
    assert {f.get("launchctl") for f in facts} == {
        "loaded",
        "not_loaded",
        "unavailable",
    }
    for key in (
        "pid_etime",
        "ticks_error",
        "heartbeat_text",
        "state_json",
        "watcher_state",
        "watcher_read_error",
        "floors_per_repo",
        "floors_error",
        "land_like_operator",
    ):
        assert any(key in f for f in facts), key
    goals = [t["expected"]["goal"] for t in ticks]
    assert any(g["scope"] for g in goals)
    assert any(g["scope_unread"] for g in goals)
    assert any(g["admissions_unread"] for g in goals)
    assert any(g["pr_reasons"] for g in goals)
    assert any(g["pr_reasons_unread"] for g in goals)


def test_a_planted_change_to_one_file_turns_the_parity_red(short_root: Path) -> None:
    index = next(
        i
        for i, t in enumerate(json.loads(_RAW)["ticks"])
        if "watcher_state" in t["expected"]["facts"]
    )
    tick = _tick(index, short_root)
    request = _build(tick, short_root)
    watcher = short_root / "watcher" / "state.json"
    state = json.loads(watcher.read_text(encoding="utf-8"))
    state["prs"]["omnimarket#1"]["facts"]["author"] = "planted-author"
    watcher.write_text(json.dumps(state), encoding="utf-8")
    result = HandlerThroughputTickFacts().handle(request)
    assert result.facts.model_dump(exclude_unset=True) != tick["expected"]["facts"]


def test_golden_a_scripted_host_gives_the_loaded_controller_facts() -> None:
    controller = "ai.omninode.landing-controller"
    source = ScriptedTickFactsSource(
        files={
            "/l/ticks.jsonl": '{"n": 1}\n{"n": 2, "pr_reasons": {"o/r#1": "queued"}}\n',
            "/l/heartbeat": "2026-11-14T05:00:00Z pid=1\n",
            "/l/state.json": '{"a": 1}',
            "/w/state.json": '{"prs": {"r#1": {"facts": {"author": "alice"}}, "r#2": {"facts": {}}}}',
            "/f.json": '{"default_per_hour": 1, "per_repo": {"r": 3}}',
        },
        programs={
            "launchctl": ModelProcessOutcome(
                returncode=0, stdout=f'{{"Label" = "{controller}"; "PID" = 4242;}};'
            ),
            "ps": ModelProcessOutcome(returncode=0, stdout=" 12:34\n"),
        },
    )
    request = ModelThroughputTickFactsRequest(
        now="2026-11-14T05:00:00Z",
        landing_state_dir="/l",
        watcher_state_path="/w/state.json",
        floors_path="/f.json",
        state_dir="/s",
        open_basis="b",
    )
    facts = (
        HandlerThroughputTickFacts(source)
        .handle(request)
        .facts.model_dump(exclude_unset=True)
    )
    assert facts["launchctl"] == "loaded"
    assert facts["pid_etime"] == "12:34"
    assert [t["n"] for t in facts["ticks"]] == [2, 1]
    assert facts["floors_per_repo"] == {"r": 3.0}
    assert facts["land_like_operator"] == ["alice"]
    assert facts["open_history"] == []
    assert facts["repo_status"] == {
        "window_minutes": 60,
        "open_units": [],
    }


def test_error_chain_every_unreadable_source_is_its_error_text_not_a_raise() -> None:
    source = ScriptedTickFactsSource(
        files={
            "/w/state.json": "{",
            "/f.json": '{"default_per_hour": -1, "per_repo": {}}',
        },
        programs={"launchctl": "launchctl timed out after 2 seconds"},
        unreadable={"/l/ticks.jsonl": "Input/output error"},
    )
    request = ModelThroughputTickFactsRequest(
        now="2026-11-14T05:00:00Z",
        landing_state_dir="/l",
        watcher_state_path="/w/state.json",
        floors_path="/f.json",
        state_dir="/s",
        open_basis="b",
    )
    result = HandlerThroughputTickFacts(source).handle(request)
    facts = result.facts.model_dump(exclude_unset=True)
    assert facts["launchctl"] == "unavailable"
    assert facts["launchctl_error"] == "launchctl timed out after 2 seconds"
    assert facts["ticks"] == []
    assert facts["ticks_error"] == "Input/output error"
    assert facts["watcher_read_error"].startswith("Expecting property name")
    assert (
        facts["floors_error"]
        == "cannot read floors /f.json: default_per_hour must be a nonnegative finite number"
    )
    assert "land_like_operator" not in facts
    assert result.goal.scope is None
    assert result.goal.scope_unread.startswith(
        "no goal_scope/overlay.yaml under ONEX_SKILL_OVERLAY_ROOTS=unset"
    )
    assert result.goal.pr_reasons is None
    assert result.goal.pr_reasons_unread == "ticks.jsonl: Input/output error".replace(
        "ticks.jsonl", "/l/ticks.jsonl"
    )


def test_error_chain_no_floors_path_is_a_floors_error() -> None:
    request = ModelThroughputTickFactsRequest(
        now="2026-11-14T05:00:00Z",
        landing_state_dir="/l",
        state_dir="/s",
        open_basis="b",
    )
    facts = (
        HandlerThroughputTickFacts(ScriptedTickFactsSource())
        .handle(request)
        .facts.model_dump(exclude_unset=True)
    )
    assert (
        facts["floors_error"] == "cannot read floors: the request names no floors_path"
    )
    assert facts["launchctl"] == "unavailable"
