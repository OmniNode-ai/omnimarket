# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The closeout record of a killed lane carries the guard's end record, and a tree the end record says holds
unpushed work is removed only once that work is pushed.

The seam with the lane guard is a file: the guard writes ``<unit>.json`` into its record directory and the
sweep reads it by the unit's lane prefix. The seam test runs the guard's real program, so it goes red when
either side changes the file's name or shape.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from omnimarket.models.lane_closeout.model_lane_closeout_record import (
    ModelLaneCloseoutRecord,
)
from omnimarket.models.lane_closeout.model_lane_guard_end_record import (
    ModelLaneGuardEndRecord,
)
from omnimarket.nodes.node_lane_guard_unit_effect import (
    runtime_lane_guard_host as guard,
)
from tests.nodes.lane_closeout_support import commit, git
from tests.nodes.node_lane_closeout_sweep_effect.test_golden_chain_lane_closeout_sweep import (
    FILL_LANE,
    Host,
    settings,
    sweep,
)

pytestmark = pytest.mark.unit

RUN = f"rlane-{FILL_LANE}-abc123"
MEM_LIMIT = 128 * 1024**2


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }.items():
        monkeypatch.setenv(key, value)


def with_records(tmp_path: Path) -> dict[str, object]:
    return {
        "settings": settings(tmp_path).model_copy(
            update={"end_records_dir": str(tmp_path / "guard-records")}
        )
    }


def plant(
    tmp_path: Path,
    tree: Path,
    *,
    unpushed: int = 0,
    changed: int = 0,
    lane: str = FILL_LANE,
    name: str | None = None,
) -> Path:
    """The file the guard writes on a kill: built by the guard's own function, then validated by the model."""
    unit = name or f"{guard.unit_name(lane, 'rlane-x-abc123')}"
    record = guard.build_end_record(
        unit=unit,
        lane=lane,
        host="h000",
        mode=guard.MODE_SCOPE,
        cause="oom_kill",
        rc=-9,
        signal=9,
        mem_peak_bytes=MEM_LIMIT,
        mem_limit_bytes=MEM_LIMIT,
        mem_enforced=True,
        runtime_s=4.0,
        runtime_limit_s=600,
        ended_at="2026-11-11T18:00:00Z",
        trees=[
            {
                "path": str(tree),
                "branch": "lane-x",
                "last_pushed_head": "a" * 40,
                "unpushed_commits": unpushed,
                "changed_files": changed,
                "readable": True,
                "error": "",
            }
        ],
    )
    ModelLaneGuardEndRecord.model_validate(record)
    directory = tmp_path / "guard-records"
    directory.mkdir(exist_ok=True)
    path = directory / f"{unit}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_end_record_golden_a_killed_lane_closeout_record_carries_cause_peak_and_limit(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    tree = host.lane(FILL_LANE)
    plant(tmp_path, tree)
    got = sweep(tmp_path, **with_records(tmp_path))
    assert not tree.exists()
    written = host.record(RUN)
    assert written["end_record"]["cause"] == "oom_kill"
    assert written["end_record"]["mem_peak_bytes"] == MEM_LIMIT
    assert written["end_record"]["mem_limit_bytes"] == MEM_LIMIT
    assert got.closed[0].end_record is not None
    assert got.closed[0].complete is True


def test_end_record_a_tree_with_an_unpushed_commit_is_kept_until_it_is_pushed(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    tree = host.lane(FILL_LANE)
    commit(tree)
    plant(tmp_path, tree, unpushed=1)
    git(tree, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    first = sweep(tmp_path, **with_records(tmp_path))
    assert tree.exists()  # the push failed, so the unpushed commit is the only copy
    record = host.record(RUN)
    assert record["complete"] is False
    assert record["trees"][0]["removed"] is False
    assert "push-failed" in record["trees"][0]["cell"]
    assert record["end_record"]["trees"][0]["unpushed_commits"] == 1
    assert first.closed[0].trees[0].removed is False
    git(tree, "remote", "set-url", "origin", str(host.origin))
    second = sweep(tmp_path, **with_records(tmp_path))
    assert not tree.exists()
    assert second.closed[0].complete is True
    assert git(
        host.origin, "for-each-ref", "--format=%(refname:short)", "refs/heads/wip/"
    )


def test_end_record_a_record_naming_work_the_tree_no_longer_shows_is_not_trusted(
    tmp_path: Path,
) -> None:
    """The record says one unpushed commit; git now says none and nothing was pushed: the tree stays."""
    host = Host(tmp_path)
    tree = host.lane(FILL_LANE)
    plant(tmp_path, tree, unpushed=1)
    sweep(tmp_path, **with_records(tmp_path))
    assert tree.exists()
    assert "end-record-unpushed-work" in host.record(RUN)["trees"][0]["cell"]


def test_end_record_the_record_field_is_in_the_written_closeout_record(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    tree = host.lane(FILL_LANE)
    plant(tmp_path, tree, unpushed=2, changed=3)
    sweep(tmp_path, **with_records(tmp_path))
    end = host.record(RUN)["end_record"]
    assert (end["trees"][0]["unpushed_commits"], end["trees"][0]["changed_files"]) == (
        2,
        3,
    )


def test_end_record_the_guard_program_and_the_sweep_agree_on_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seam: the real guard program kills a real command and writes its record; the sweep reads that file."""
    host = Host(tmp_path)
    tree = host.lane(FILL_LANE)
    monkeypatch.setattr(
        guard,
        "gather_facts",
        lambda: {
            "linux": True,
            "systemd_ok": False,
            "controllers": [],
            "memory_controller": False,
            "timeout": False,
            "prlimit": False,
            "linger": "no",
        },
    )
    rc = guard.run_guarded(
        guard.unit_name(FILL_LANE, "rlane-x-abc123"),
        guard.Limits(1, 64 * 1024**2, 100),
        [sys.executable, "-c", "import time; time.sleep(60)"],
        FILL_LANE,
        {
            "record_dir": str(tmp_path / "guard-records"),
            "worktrees_dir": str(tmp_path / "registry" / "omni_worktrees"),
            "git_timeout_s": 20,
            "max_trees": 20,
        },
    )
    assert rc != 0
    sweep(tmp_path, **with_records(tmp_path))
    end = host.record(RUN)["end_record"]
    assert end["cause"] == "runtime_max"
    assert end["trees"][0]["path"] == str(tree)
    assert end["trees"][0]["branch"] == f"lane-{FILL_LANE}"
    assert not tree.exists()


def test_end_record_a_corrupt_file_is_named_without_its_content_and_the_lane_still_closes(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    tree = host.lane(FILL_LANE)
    (tmp_path / "guard-records").mkdir()
    bad = (
        tmp_path
        / "guard-records"
        / f"{guard.unit_name(FILL_LANE, 'rlane-x-ffffff')}.json"
    )
    bad.write_text("not json SECRET-CONTENT", encoding="utf-8")
    sweep(tmp_path, **with_records(tmp_path))
    record = host.record(RUN)
    assert record["end_record"] is None
    assert bad.name in record["end_record_error"]
    assert "SECRET-CONTENT" not in json.dumps(record)
    assert not tree.exists()


def test_end_record_a_corrupt_file_does_not_hide_a_valid_one(tmp_path: Path) -> None:
    host = Host(tmp_path)
    tree = host.lane(FILL_LANE)
    plant(tmp_path, tree)
    bad = (
        tmp_path
        / "guard-records"
        / f"{guard.unit_name(FILL_LANE, 'rlane-x-ffffff')}.json"
    )
    bad.write_text("{", encoding="utf-8")
    sweep(tmp_path, **with_records(tmp_path))
    record = host.record(RUN)
    assert record["end_record"]["cause"] == "oom_kill"
    assert record["end_record_error"] == ""


def test_end_record_another_lanes_record_is_not_attached(tmp_path: Path) -> None:
    host = Host(tmp_path)
    tree = host.lane(FILL_LANE)
    plant(tmp_path, tree, lane="some-other-lane")
    sweep(tmp_path, **with_records(tmp_path))
    record = host.record(RUN)
    assert record["end_record"] is None
    assert record["end_record_error"] == ""


def test_end_record_no_directory_or_no_setting_reads_none(tmp_path: Path) -> None:
    host = Host(tmp_path)
    host.lane(FILL_LANE)
    sweep(tmp_path, **with_records(tmp_path))  # the directory does not exist
    assert host.record(RUN)["end_record"] is None
    (tmp_path / "second").mkdir()
    host2 = Host(tmp_path / "second")
    tree = host2.lane(FILL_LANE)
    plant(tmp_path / "second", tree)
    sweep(tmp_path / "second")  # the overlay names no end_records_dir
    assert host2.record(RUN)["end_record"] is None


def test_end_record_a_closeout_record_written_before_the_field_still_loads() -> None:
    old = {
        "lane": "x",
        "run": "rlane-x-abc123",
        "host": "h000",
        "closed_at": "2026-11-11T18:00:00Z",
        "ended_by": "run.rc",
        "trees": [],
        "run_dir_removed": True,
        "complete": True,
        "error": "",
    }
    loaded = ModelLaneCloseoutRecord.model_validate(old)
    assert loaded.end_record is None
    assert loaded.end_record_error == ""
