# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The guard writes one typed end record when it kills a lane's command, and none on a clean exit.

The record is read-only facts about the dead lane (cause, memory peak against the limit, runtime, and per tree
the branch, last pushed head, unpushed commit count and changed file count). The fixture of a memory kill is the
shape systemd reports for a failed scope: result ``oom-kill`` with a memory peak at the limit.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.lane_closeout.model_lane_guard_end_record import (
    EnumLaneGuardEndCause,
    ModelLaneGuardEndRecord,
)
from omnimarket.models.lane_closeout.model_lane_guard_end_record_settings import (
    ModelLaneGuardEndRecordSettings,
)
from omnimarket.nodes.node_lane_guard_unit_effect import (
    runtime_lane_guard_host as host,
)
from omnimarket.nodes.node_lane_guard_unit_effect.handlers import (
    handler_lane_guard_unit as handler_module,
)
from omnimarket.nodes.node_lane_guard_unit_effect.handlers.handler_lane_guard_unit import (
    HandlerLaneGuardUnit,
    load_guard_overlay,
)
from omnimarket.nodes.node_lane_guard_unit_effect.models import (
    EnumLaneGuardOperation,
    ModelLaneGuardUnitRequest,
)
from tests.nodes.lane_closeout_support import (
    GIT_ENV,
    add_worktree,
    clone,
    commit,
    git,
    make_origin,
)

MEM_LIMIT = 128 * 1024**2
NO_SYSTEMD = {
    "linux": True,
    "systemd_ok": False,
    "controllers": [],
    "memory_controller": False,
    "timeout": False,
    "prlimit": False,
    "linger": "no",
}


def _settings(tmp_path: Path) -> dict[str, object]:
    return {
        "record_dir": str(tmp_path / "records"),
        "worktrees_dir": str(tmp_path / "omni_worktrees"),
        "git_timeout_s": 20,
        "max_trees": 20,
    }


def _record(**over: Any) -> dict[str, object]:
    fields: dict[str, Any] = {
        "unit": "onexlane-fixture-abc",
        "lane": "fixture",
        "host": "h000",
        "mode": host.MODE_SCOPE,
        "cause": "oom_kill",
        "rc": -9,
        "signal": 9,
        "mem_peak_bytes": MEM_LIMIT,
        "mem_limit_bytes": MEM_LIMIT,
        "mem_enforced": True,
        "runtime_s": 3.2,
        "runtime_limit_s": 600,
        "ended_at": "2026-11-11T18:00:00Z",
        "trees": [],
    }
    fields.update(over)
    return host.build_end_record(**fields)


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in GIT_ENV.items():
        monkeypatch.setenv(key, value)


def _guarded_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    runtime_s: int,
    lane: str = "fixture",
) -> int:
    monkeypatch.setattr(host, "gather_facts", lambda: dict(NO_SYSTEMD))
    limits = host.Limits(runtime_s, 64 * 1024**2, 100)
    return host.run_guarded(
        f"{host.UNIT_PREFIX}{lane}-abc123",
        limits,
        command,
        lane,
        _settings(tmp_path),
    )


def _records(tmp_path: Path) -> list[Path]:
    folder = tmp_path / "records"
    return sorted(folder.glob("*.json")) if folder.is_dir() else []


def test_end_record_a_memory_kill_names_oom_kill_with_the_peak_and_the_limit() -> None:
    """The fixture of a failed scope: result oom-kill and a peak at the limit."""
    assert host.classify_end(-9, "oom-kill", False, None, 1) == "oom_kill"
    model = ModelLaneGuardEndRecord.model_validate(_record())
    assert model.cause is EnumLaneGuardEndCause.OOM_KILL
    assert (model.mem_peak_bytes, model.mem_limit_bytes) == (MEM_LIMIT, MEM_LIMIT)
    assert model.mem_enforced is True


def test_end_record_a_runtime_limit_kill_writes_runtime_max(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert host.classify_end(-15, "timeout", True, None, 0) == "runtime_max"
    rc = _guarded_run(
        tmp_path,
        monkeypatch,
        [sys.executable, "-c", "import time; time.sleep(60)"],
        runtime_s=1,
    )
    assert rc != 0
    written = _records(tmp_path)
    assert [p.name for p in written] == ["onexlane-fixture-abc123.json"]
    model = ModelLaneGuardEndRecord.model_validate_json(
        written[0].read_text(encoding="utf-8")
    )
    assert model.cause is EnumLaneGuardEndCause.RUNTIME_MAX
    assert (
        model.mem_enforced is False
    )  # the watchdog mode has no memory limit, and the record says so
    assert model.runtime_limit_s == 1


def test_end_record_a_clean_exit_writes_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert host.classify_end(0, None, False, None, 0) is None
    rc = _guarded_run(tmp_path, monkeypatch, [sys.executable, "-c", "pass"], 30)
    assert rc == 0
    assert _records(tmp_path) == []


def test_end_record_a_nonzero_exit_and_a_signal_are_told_apart() -> None:
    assert host.classify_end(3, None, False, None, 0) == "exit_code"
    assert host.classify_end(-9, None, False, None, 0) == "signal"
    assert host.classify_end(0, None, False, 15, 0) == "signal"


def test_end_record_lists_each_tree_read_only(tmp_path: Path) -> None:
    origin = make_origin(tmp_path)
    canon = clone(tmp_path, origin, tmp_path / "canon")
    lane_dir = tmp_path / "omni_worktrees" / "fixture"
    tree = add_worktree(canon, lane_dir / "repo", "lane-fixture")
    commit(tree)
    (tree / "loose.txt").write_text("x\n", encoding="utf-8")
    (lane_dir / "not-a-tree").mkdir()
    before = (git(tree, "status", "--porcelain"), git(tree, "rev-parse", "HEAD"))
    trees = host.find_trees(str(tmp_path / "omni_worktrees"), "fixture", 20)
    assert trees == [str(lane_dir / "repo")]
    read = host.read_tree(trees[0], 20)
    assert read["branch"] == "lane-fixture"
    assert read["last_pushed_head"] == git(origin, "rev-parse", "main")
    assert (read["unpushed_commits"], read["changed_files"]) == (1, 1)
    assert read["readable"] is True
    after = (git(tree, "status", "--porcelain"), git(tree, "rev-parse", "HEAD"))
    assert before == after


def test_end_record_an_unreadable_tree_is_marked_not_raised(tmp_path: Path) -> None:
    not_git = tmp_path / "omni_worktrees" / "fixture" / "plain"
    not_git.mkdir(parents=True)
    read = host.read_tree(str(not_git), 20)
    assert read["readable"] is False
    assert (read["unpushed_commits"], read["changed_files"]) == (0, 0)
    assert read["error"]


def test_end_record_the_tree_cap_and_depth_follow_the_closeout_inventory(
    tmp_path: Path,
) -> None:
    lane_dir = tmp_path / "omni_worktrees" / "fixture"
    for name in ("a/one", "b", "c/deep/three"):
        (lane_dir / name / ".git").mkdir(parents=True)
    found = host.find_trees(str(tmp_path / "omni_worktrees"), "fixture", 20)
    assert found == [str(lane_dir / "a" / "one"), str(lane_dir / "b")]
    assert host.find_trees(str(tmp_path / "omni_worktrees"), "fixture", 1) == found[:1]
    assert host.find_trees(str(tmp_path / "omni_worktrees"), "missing", 20) == []


def test_end_record_a_write_failure_never_changes_the_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "records"
    blocker.write_text("a file where the directory should be\n", encoding="utf-8")
    rc = _guarded_run(
        tmp_path, monkeypatch, [sys.executable, "-c", "raise SystemExit(7)"], 30
    )
    assert rc == 7
    end = [
        line
        for line in capsys.readouterr().err.splitlines()
        if line.startswith("LANE-GUARD-END ")
    ]
    payload = json.loads(end[0].split(" ", 1)[1])
    assert payload["cause"] == "exit_code"
    assert payload["end_record_error"]
    assert "end_record" not in payload


def test_end_record_the_cli_needs_all_four_settings(tmp_path: Path) -> None:
    done = subprocess.run(
        [
            sys.executable,
            str(host.__file__),
            "run",
            "--unit",
            "onexlane-fixture-abc",
            "--runtime-s",
            "5",
            "--record-dir",
            str(tmp_path / "records"),
            "--",
            sys.executable,
            "-c",
            "pass",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ},
    )
    assert done.returncode == host.EXIT_USAGE
    assert "--worktrees-dir" in done.stderr


def test_end_record_the_start_operation_passes_the_overlay_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []

    class _Spy:
        pid = 1

        def __init__(self, argv: list[str], **_: object) -> None:
            seen.append(argv)

    monkeypatch.setattr(handler_module.subprocess, "Popen", _Spy)
    settings = ModelLaneGuardEndRecordSettings.model_validate(_settings(tmp_path))
    base = {
        "operation": EnumLaneGuardOperation.START,
        "lane": "fixture",
        "run_id": "run-1",
        "command": ("true",),
    }
    HandlerLaneGuardUnit().handle(
        ModelLaneGuardUnitRequest(**base, end_record=settings)
    )
    HandlerLaneGuardUnit().handle(ModelLaneGuardUnitRequest(**base))
    with_record, without = seen
    assert with_record[with_record.index("--record-dir") + 1] == settings.record_dir
    assert with_record.index("--max-trees") < with_record.index("--")
    assert not {"--record-dir", "--worktrees-dir", "--max-trees"} & set(without)


def test_end_record_the_overlay_example_loads_through_the_node_loader(
    tmp_path: Path,
) -> None:
    root = tmp_path / "overlays" / "node_lane_guard_unit_effect"
    root.mkdir(parents=True)
    example = Path(handler_module.__file__).parents[1] / "overlay.example.yaml"
    (root / "overlay.yaml").write_text(
        example.read_text(encoding="utf-8"), encoding="utf-8"
    )
    loaded = load_guard_overlay(
        {"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "overlays")}
    )
    assert loaded.end_record.max_trees == 20
