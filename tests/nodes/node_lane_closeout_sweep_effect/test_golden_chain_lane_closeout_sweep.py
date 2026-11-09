# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host-local sweep closes out ended lanes of every launcher, salvages dirty trees, and leaves a record (OMN-23375)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.lane_closeout.enum_lane_guard_verdict import EnumLaneGuardVerdict
from omnimarket.models.lane_closeout.model_lane_closeout_sweep_request import (
    ModelLaneCloseoutSweepRequest,
)
from omnimarket.models.lane_closeout.model_lane_closeout_sweep_result import (
    ModelLaneCloseoutSweepResult,
)
from omnimarket.models.lane_closeout.model_lane_closeout_sweep_settings import (
    ModelLaneCloseoutSweepSettings,
)
from omnimarket.nodes.node_lane_closeout_sweep_effect.handlers.handler_lane_closeout_sweep import (
    HandlerLaneCloseoutSweep,
)
from tests.nodes.lane_closeout_support import (
    add_worktree,
    clone,
    git,
    make_origin,
    run_over_profile,
)

pytestmark = pytest.mark.unit

NOW = 2_000_000_000.0
HOUR = 3600
FILL_LANE = "fill-ticket-OMN-22697-20261109T1419Z"


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }.items():
        monkeypatch.setenv(key, value)


def settings(tmp_path: Path) -> ModelLaneCloseoutSweepSettings:
    return ModelLaneCloseoutSweepSettings(
        runs_root=str(tmp_path / "runs"),
        worktrees_dir="omni_worktrees",
        records_dir="records",
        ctx_suffix=".ctx",
        run_prefixes=("rlane-",),
        finished_markers=("run.rc", "engine.json"),
        grace_seconds=600,
        orphan_age_seconds=6 * HOUR,
        max_lanes_per_sweep=50,
        salvage_branch_prefix="salvage",
    )


def request(tmp_path: Path, **over: object) -> ModelLaneCloseoutSweepRequest:
    fields: dict[str, object] = {
        "registry_root": str(tmp_path / "registry"),
        "state_dir": str(tmp_path / "state"),
        "host": "h053",
        "settings": settings(tmp_path),
        "now": NOW,
    }
    fields.update(over)
    return ModelLaneCloseoutSweepRequest.model_validate(fields)


class Host:
    """One lab host's registry root and run directory, with a bare origin the lane trees push to."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.origin = make_origin(tmp_path)
        self.canon = clone(tmp_path, self.origin, tmp_path / "canon")
        self.runs = tmp_path / "runs"
        self.runs.mkdir()

    def lane(
        self,
        lane: str,
        *,
        ctx: str | None = "rlane-{lane}-abc123",
        marker: str | None = "run.rc",
        ended_s_ago: float = 2 * HOUR,
        dirty: bool = False,
    ) -> Path:
        tree = add_worktree(
            self.canon,
            self.tmp_path / "registry" / "omni_worktrees" / lane / "repo",
            f"lane-{lane}",
        )
        if dirty:
            (tree / "new_test.py").write_text("x = 1\n", encoding="utf-8")
            git(tree, "add", "new_test.py")
            (tree / "f.txt").write_text("edited\n", encoding="utf-8")
        if ctx is not None:
            name = ctx.format(lane=lane)
            (self.runs / name).mkdir()
            context = self.runs / f"{name}.ctx"
            context.mkdir()
            (context / "run.out").write_text("out\n", encoding="utf-8")
            if marker:
                (context / marker).write_text("0", encoding="utf-8")
            when = NOW - ended_s_ago
            for path in (
                context / "run.out",
                context / marker if marker else context,
                context,
            ):
                os.utime(path, (when, when))
        return tree

    def record(self, name: str) -> dict[str, Any]:
        path = self.tmp_path / "state" / "records" / f"{name}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def records(self) -> list[str]:
        folder = self.tmp_path / "state" / "records"
        return sorted(p.name for p in folder.glob("*.json")) if folder.is_dir() else []

    def salvage_branches(self) -> list[str]:
        out = git(
            self.origin,
            "for-each-ref",
            "--format=%(refname:short)",
            "refs/heads/salvage/",
        )
        return out.splitlines()


def sweep(tmp_path: Path, **over: object) -> ModelLaneCloseoutSweepResult:
    return HandlerLaneCloseoutSweep(live_cwds=lambda: []).handle(
        request(tmp_path, **over)
    )


def test_a_fill_launched_lane_has_no_closeout_record_until_the_sweep_runs(
    tmp_path: Path,
) -> None:
    """The failing control: the reading names the ended fill lane as unrecorded and changes nothing."""
    host = Host(tmp_path)
    tree = host.lane(FILL_LANE)
    got = sweep(tmp_path, read_only=True)
    assert got.ended_lanes == 1
    assert (got.unrecorded, got.unrecorded_lanes) == (1, (FILL_LANE,))
    assert host.records() == []
    assert tree.exists()


def test_the_sweep_removes_a_clean_fill_lane_and_its_run_directory_and_records_it(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    tree = host.lane(FILL_LANE)
    got = sweep(tmp_path)
    assert got.unrecorded == 0
    assert not tree.exists()
    assert not (host.runs / f"rlane-{FILL_LANE}-abc123").exists()
    assert not (host.runs / f"rlane-{FILL_LANE}-abc123.ctx").exists()
    record = host.record(f"rlane-{FILL_LANE}-abc123")
    assert (record["lane"], record["host"], record["complete"]) == (
        FILL_LANE,
        "h053",
        True,
    )
    assert record["ended_by"] == "run.rc"
    assert record["trees"][0]["cell"] == f"worktree=removed:{tree}"


def test_a_dirty_tree_is_pushed_to_a_salvage_branch_then_removed(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    tree = host.lane(FILL_LANE, dirty=True)
    got = sweep(tmp_path)
    tag = f"{FILL_LANE}-rlane-{FILL_LANE}-abc123-repo"
    assert host.salvage_branches() == [f"salvage/{tag}"]
    pushed = git(host.origin, "show", f"salvage/{tag}:new_test.py")
    assert pushed == "x = 1"
    assert git(host.origin, "show", f"salvage/{tag}:f.txt") == "edited"
    assert not tree.exists()
    outcome = got.closed[0].trees[0]
    assert (outcome.verdict, outcome.pushed_branch) == (
        EnumLaneGuardVerdict.WIP,
        f"salvage/{tag}",
    )
    assert got.closed[0].complete is True


def test_a_dirty_tree_whose_push_fails_is_kept_with_its_reason_and_retried_next_sweep(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    tree = host.lane(FILL_LANE, dirty=True)
    git(tree, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    first = sweep(tmp_path)
    assert tree.exists()
    assert first.unrecorded == 0  # kept with a recorded reason is a record
    record = host.record(f"rlane-{FILL_LANE}-abc123")
    assert record["complete"] is False
    assert "salvage-push-failed" in record["trees"][0]["cell"]
    assert (
        host.runs / f"rlane-{FILL_LANE}-abc123.ctx"
    ).exists()  # the end marker stays for the retry
    git(tree, "remote", "set-url", "origin", str(host.origin))
    second = sweep(tmp_path)
    assert not tree.exists()
    assert second.closed[0].complete is True
    assert host.record(f"rlane-{FILL_LANE}-abc123")["complete"] is True


def test_a_running_lane_and_a_lane_inside_its_grace_are_left_alone(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    running = host.lane("running-lane", marker=None, ended_s_ago=60)
    fresh = host.lane("just-ended", ended_s_ago=60)
    got = sweep(tmp_path)
    assert running.exists()
    assert fresh.exists()
    assert got.closed == ()
    assert got.ended_lanes == 0


def test_a_lane_with_a_live_process_inside_its_tree_is_skipped(tmp_path: Path) -> None:
    host = Host(tmp_path)
    tree = host.lane(FILL_LANE)
    got = HandlerLaneCloseoutSweep(live_cwds=lambda: [str(tree / "sub")]).handle(
        request(tmp_path)
    )
    assert tree.exists()
    assert got.skipped_live == (FILL_LANE,)
    assert got.unrecorded == 1


def test_a_stuck_lane_with_no_end_marker_is_closed_once_it_is_past_the_orphan_age(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    tree = host.lane("stuck-lane", marker=None, ended_s_ago=7 * HOUR)
    got = sweep(tmp_path)
    assert not tree.exists()
    assert got.closed[0].ended_by == "orphan-age"


def test_a_lane_directory_with_no_run_directory_is_closed_by_age(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path)
    old = host.lane("no-ctx-lane", ctx=None)
    young = host.lane("no-ctx-young", ctx=None)
    long_ago = NOW - 7 * HOUR
    os.utime(old.parent, (long_ago, long_ago))
    os.utime(old, (long_ago, long_ago))
    os.utime(young.parent, (NOW - 60, NOW - 60))
    got = sweep(tmp_path)
    assert not old.exists()
    assert young.exists()
    assert got.closed[0].ended_by == "orphan-lane-dir"
    assert host.records() == ["no-ctx-lane.json"]


def test_a_landing_worker_lane_is_read_from_its_engine_json(tmp_path: Path) -> None:
    host = Host(tmp_path)
    tree = host.lane("landing-L26763", ctx="{lane}-abc123", marker="engine.json")
    # the landing worker's context directory carries no run prefix
    got = sweep(tmp_path)
    assert not tree.exists()
    assert got.closed[0].lane == "landing-L26763"
    assert got.closed[0].ended_by == "engine.json"


def test_a_named_lane_sweeps_only_that_lane(tmp_path: Path) -> None:
    host = Host(tmp_path)
    mine = host.lane(FILL_LANE)
    other = host.lane("other-lane")
    sweep(tmp_path, lane=FILL_LANE)
    assert not mine.exists()
    assert other.exists()


def test_the_reading_goes_red_when_the_record_write_is_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Named injected break: the record is never written, so the reading must say so."""
    host = Host(tmp_path)
    host.lane(FILL_LANE)

    def never_written(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(HandlerLaneCloseoutSweep, "_write_record", never_written)
    got = sweep(tmp_path)
    assert got.unrecorded == 1
    assert got.unrecorded_lanes == (FILL_LANE,)


def test_a_relative_registry_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        sweep(tmp_path, registry_root="registry")


async def test_the_sweep_over_the_inmemory_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host(tmp_path)
    tree = host.lane(FILL_LANE, dirty=True)
    req = request(tmp_path, now=2_000_000_000.0)
    runtime = await run_over_profile(
        "node_lane_closeout_sweep_effect", req, "inmemory", tmp_path, monkeypatch
    )
    result = runtime.handler_result
    assert result.correlation_id == req.correlation_id
    assert result.unrecorded == 0
    assert not tree.exists()
    assert len(host.salvage_branches()) == 1
    assert (
        runtime._contract["terminal_event"]
        == "onex.evt.omnimarket.lane-closeout-sweep-completed.v1"
    )


def test_the_tick_reads_exit_3_for_an_unrecorded_lane_and_0_once_swept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reading the unit's next tick makes: red while an ended lane has no record, green after the sweep."""
    from omnimarket.nodes.node_lane_closeout_sweep_effect import (
        runtime_lane_closeout_sweep as tick,
    )

    host = Host(tmp_path)
    host.lane(FILL_LANE)
    overlay_root = tmp_path / "overlays"
    (overlay_root / tick.NODE).mkdir(parents=True)
    (overlay_root / tick.NODE / "overlay.yaml").write_text(
        settings(tmp_path).model_dump_json(), encoding="utf-8"
    )
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(overlay_root))
    monkeypatch.setenv("LAB_WORK_HOST_NAME", "h053")
    monkeypatch.setattr("time.time", lambda: NOW)
    argv = [
        "--omni-home",
        str(tmp_path / "registry"),
        "--state-dir",
        str(tmp_path / "state"),
    ]
    assert tick.main([*argv, "--read"]) == tick.EXIT_UNRECORDED
    assert json.loads(capsys.readouterr().out)["unrecorded_lanes"] == [FILL_LANE]
    monkeypatch.setattr(
        "omnimarket.nodes.node_lane_closeout_sweep_effect.runtime_lane_closeout_sweep.HandlerLaneCloseoutSweep",
        lambda: HandlerLaneCloseoutSweep(live_cwds=lambda: []),
    )
    assert tick.main(argv) == tick.EXIT_OK
    assert json.loads(capsys.readouterr().out)["closed_complete"] == 1
    assert tick.main([*argv, "--read"]) == tick.EXIT_OK
    assert (tmp_path / "state" / "last-run.json").is_file()
