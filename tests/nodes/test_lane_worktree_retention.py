# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane worktree retention under ``~/lab-run/runs`` (node_retention_cleanup).

Recorded case, h101 2026-11-05: ``~/lab-run/runs`` held 349 lane worktrees (139 GB) and 466
``.ctx`` directories (85 GB), and nothing freed them: the file retention entry
``lab-run-lane-dirs`` skips any unit holding a ``.git`` entry, and no retention job ran there.

A lane worktree goes when it is clean, every local commit is on a remote, no live process names
it and nothing in it changed within the idle bound. A ``.ctx`` directory goes after seven days
unless a live process names it or its run tree. With no readable process table, nothing goes.
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_retention_cleanup.handlers.handler_lane_worktree_retention import (
    HandlerLaneWorktreeRetention,
    load_lane_worktree_policy,
)
from omnimarket.nodes.node_retention_cleanup.models.model_lane_worktree_retention import (
    ModelLaneWorktreeRetentionRequest,
    ModelLaneWorktreeRetentionResult,
)

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 11, 5, 1, 10, tzinfo=dt.UTC)
OLD = (NOW - dt.timedelta(days=10)).timestamp()
FRESH = (NOW - dt.timedelta(hours=2)).timestamp()


def _git(cwd: Path, *args: str) -> str:
    env = scrub_git_location_env(os.environ)
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        env=scrub_git_location_env(env),
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _age(tree: Path, stamp: float) -> None:
    for root, dirs, files in os.walk(tree):
        for name in (*dirs, *files):
            os.utime(Path(root) / name, (stamp, stamp), follow_symlinks=False)
    os.utime(tree, (stamp, stamp))


@pytest.fixture
def lab(tmp_path: Path) -> dict[str, Path]:
    """A bare origin, a canonical clone with a pushed commit, and ~/lab-run/runs."""
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "dev", str(origin)],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    clone = tmp_path / "clone"
    subprocess.run(
        ["git", "init", "-q", "-b", "dev", str(clone)],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    (clone / "README.md").write_text("x\n")
    _git(clone, "add", "README.md")
    _git(clone, "commit", "-q", "-m", "init")
    _git(clone, "remote", "add", "origin", str(origin))
    _git(clone, "push", "-q", "origin", "dev")
    runs = tmp_path / "lab-run" / "runs"
    runs.mkdir(parents=True)
    return {"origin": origin, "clone": clone, "runs": runs}


def _linked(lab: dict[str, Path], run: str, *, age: float = OLD) -> Path:
    tree = lab["runs"] / run / "omnimarket"
    tree.parent.mkdir()
    _git(lab["clone"], "worktree", "add", "-q", "-b", run, str(tree), "origin/dev")
    _age(tree.parent, age)
    return tree.parent


def _cloned(lab: dict[str, Path], run: str, *, age: float = OLD) -> Path:
    unit = lab["runs"] / run
    subprocess.run(
        ["git", "clone", "-q", str(lab["origin"]), str(unit)],
        check=True,
        capture_output=True,
        env=scrub_git_location_env(os.environ),
    )
    _age(unit, age)
    return unit


def _ctx(lab: dict[str, Path], run: str, *, age: float) -> Path:
    ctx = lab["runs"] / f"{run}.ctx"
    ctx.mkdir()
    (ctx / "prompt.md").write_text("brief\n")
    _age(ctx, age)
    return ctx


def _run(
    lab: dict[str, Path], refs: list[str] | None = None, *, dry_run: bool = False
) -> ModelLaneWorktreeRetentionResult:
    references = [] if refs is None else refs
    return HandlerLaneWorktreeRetention(process_references=lambda: references).handle(
        ModelLaneWorktreeRetentionRequest(
            runs_dir=lab["runs"], now=NOW, idle_days=3, ctx_keep_days=7, dry_run=dry_run
        )
    )


def test_a_clean_pushed_idle_worktree_and_its_old_ctx_are_removed(
    lab: dict[str, Path],
) -> None:
    linked = _linked(lab, "rlane-a")
    cloned = _cloned(lab, "rlane-b")
    ctx = _ctx(lab, "rlane-a", age=OLD)

    result = _run(lab)

    assert result.status == "ran", result
    assert set(result.removed_worktrees) == {"rlane-a", "rlane-b"}
    assert result.removed_ctx == ("rlane-a.ctx",)
    assert not linked.exists()
    assert not cloned.exists()
    assert not ctx.exists()
    assert result.freed_bytes > 0
    # The canonical clone no longer lists the removed linked worktree.
    assert "rlane-a" not in _git(lab["clone"], "worktree", "list")


@pytest.mark.asyncio
async def test_lane_retention_runs_through_its_contract_bus_route(
    tmp_path: Path,
) -> None:
    """The declared operation reaches the typed handler and emits its result."""
    from uuid import uuid4

    from omnimarket.adapters.codex.local_runtime_dispatch import LocalRuntimeDispatch
    from omnimarket.adapters.codex.runtime_client import (
        ModelDispatchBusCommand,
        default_command_topic,
        default_response_topic,
    )

    runtime = LocalRuntimeDispatch(
        adapter_command_topic=default_command_topic(),
        state_root=tmp_path / "runtime",
    )
    terminal, evidence = await runtime.dispatch(
        ModelDispatchBusCommand(
            command_name="node_retention_cleanup.lane_worktree_retention",
            requester="lane-retention-test",
            correlation_id=uuid4(),
            payload=ModelLaneWorktreeRetentionRequest(
                runs_dir=tmp_path / "absent-runs", now=NOW, dry_run=True
            ).model_dump(mode="json"),
            response_topic=default_response_topic(),
            timeout_seconds=10,
        )
    )
    assert terminal.status == "completed", terminal.error_message
    assert terminal.payload["status"] == "absent"
    assert evidence.runtime_observation.status == "OBSERVED"
    assert evidence.adapter_dispatch_binding.node_contract.status == "RESOLVED"
    assert evidence.handler_route.endswith("HandlerLaneWorktreeRetention")


@pytest.mark.parametrize(
    "command", ["node_retention_cleanup", "node_retention_cleanup.retention_cleanup"]
)
def test_database_commands_keep_their_database_request_model(command: str) -> None:
    from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route

    route = _resolve_node_route(command)
    assert route.handler_class == "NodeRetentionCleanup"
    assert route.input_model_name == "RetentionCleanupRequest"


def test_uncommitted_unpushed_live_and_recent_worktrees_are_kept(
    lab: dict[str, Path],
) -> None:
    dirty = _linked(lab, "rlane-dirty")
    (dirty / "omnimarket" / "new.py").write_text("pass\n")
    _age(dirty, OLD)
    unpushed = _linked(lab, "rlane-unpushed")
    (unpushed / "omnimarket" / "README.md").write_text("y\n")
    _git(unpushed / "omnimarket", "commit", "-q", "-am", "local")
    _age(unpushed, OLD)
    live = _linked(lab, "rlane-live")
    recent = _linked(lab, "rlane-recent", age=FRESH)

    result = _run(lab, [f"/usr/bin/python3 -m pytest {live}/omnimarket/tests"])

    assert result.removed_worktrees == ()
    assert result.kept["rlane-dirty"].startswith("uncommitted-changes")
    assert result.kept["rlane-unpushed"].startswith("unpushed-commits")
    assert result.kept["rlane-live"] == "in-use"
    assert result.kept["rlane-recent"] == "active"
    assert all(tree.exists() for tree in (dirty, unpushed, live, recent))


def test_a_clone_holding_a_stash_or_a_linked_worktree_is_kept(
    lab: dict[str, Path],
) -> None:
    stashed = _cloned(lab, "rlane-stash", age=FRESH)
    (stashed / "README.md").write_text("stashed\n")
    _git(stashed, "stash", "-q")
    _age(stashed, OLD)
    holder = _cloned(lab, "rlane-holder", age=FRESH)
    _git(holder, "worktree", "add", "-q", str(lab["runs"].parent / "elsewhere"))
    _age(holder, OLD)

    result = _run(lab)

    assert result.kept["rlane-stash"].startswith("unpushed-commits")
    assert result.kept["rlane-holder"].startswith("holds-worktree-git-dir")
    assert stashed.exists()
    assert holder.exists()


def test_ctx_dirs_go_after_seven_days_unless_live(lab: dict[str, Path]) -> None:
    old = _ctx(lab, "rlane-old", age=OLD)
    recent = _ctx(lab, "rlane-recent", age=(NOW - dt.timedelta(days=5)).timestamp())
    held = _ctx(lab, "rlane-held", age=OLD)
    sibling = _ctx(lab, "rlane-sib", age=OLD)
    run_tree = lab["runs"] / "rlane-sib"
    run_tree.mkdir()

    result = _run(lab, [f"cat {held}/prompt.md", f"cd {run_tree}"])

    assert result.removed_ctx == ("rlane-old.ctx",)
    assert not old.exists()
    assert recent.exists()
    assert held.exists()
    assert sibling.exists()
    assert result.kept["rlane-held.ctx"] == "in-use"
    assert result.kept["rlane-sib.ctx"] == "in-use"


def test_a_run_dir_with_no_git_is_left_to_file_retention(lab: dict[str, Path]) -> None:
    plain = lab["runs"] / "rlane-plain"
    plain.mkdir()
    (plain / "out.log").write_text("x")
    _age(plain, OLD)

    result = _run(lab)

    assert plain.exists()
    assert "rlane-plain" not in result.removed_worktrees
    assert "rlane-plain" not in result.kept


def test_dry_run_names_what_would_go_and_removes_nothing(lab: dict[str, Path]) -> None:
    linked = _linked(lab, "rlane-a")
    ctx = _ctx(lab, "rlane-a", age=OLD)

    result = _run(lab, dry_run=True)

    assert result.status == "dry-run"
    assert result.removed_worktrees == ("rlane-a",)
    assert result.removed_ctx == ("rlane-a.ctx",)
    assert linked.exists()
    assert ctx.exists()


def test_an_unreadable_process_table_removes_nothing(lab: dict[str, Path]) -> None:
    linked = _linked(lab, "rlane-a")
    ctx = _ctx(lab, "rlane-a", age=OLD)

    result = HandlerLaneWorktreeRetention(process_references=lambda: None).handle(
        ModelLaneWorktreeRetentionRequest(runs_dir=lab["runs"], now=NOW)
    )

    assert result.status == "skipped"
    assert linked.exists()
    assert ctx.exists()


def test_a_symlinked_unit_is_never_followed(
    lab: dict[str, Path], tmp_path: Path
) -> None:
    target = _cloned(lab, "rlane-target")
    link = lab["runs"] / "rlane-link"
    link.symlink_to(target)
    _age(target, OLD)

    result = _run(lab, [f"cd {target}"])

    assert link.is_symlink()
    assert target.exists()
    assert "rlane-link" not in result.removed_worktrees


def test_the_contract_declares_the_bounds() -> None:
    policy = load_lane_worktree_policy()
    assert policy.runs_dir == "~/lab-run/runs"
    assert policy.ctx_keep_days == 7
    assert policy.idle_days >= 1


def test_the_cli_runs_the_pass_without_a_database(
    lab: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import sys

    from omnimarket.nodes.node_retention_cleanup import __main__ as cli

    linked = _linked(lab, "rlane-a")
    monkeypatch.setattr(
        sys,
        "argv",
        ["retention", "--lane-worktrees", "--runs-dir", str(lab["runs"]), "--dry-run"],
    )
    with pytest.raises(SystemExit) as done:
        cli.main()
    assert done.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("lane_worktrees=dry-run removed=")
    assert linked.exists()


def test_the_lab_daemons_overlay_runs_both_lab_run_passes_on_a_host_with_lab_run() -> (
    None
):
    import yaml

    from omnimarket.nodes.node_lab_daemon_sync_effect.models.model_lab_daemon_sync import (
        ModelChoreDeclaration,
    )

    overlay = (
        Path(__file__).resolve().parents[2]
        / "src/omnimarket/nodes/node_lab_daemon_sync_effect/overlays/lab_daemons.yaml"
    )
    chores = {
        chore["name"]: ModelChoreDeclaration.model_validate(chore)
        for chore in yaml.safe_load(overlay.read_text())["chores"]
    }
    lane = chores["lab-run-lane-worktrees"]
    assert lane.present_when == "${HOME}/lab-run/runs"
    assert lane.argv[1:] == [
        "-m",
        "omnimarket.nodes.node_retention_cleanup",
        "--lane-worktrees",
    ]
    snapshots = chores["lab-run-plugin-snapshots"]
    assert snapshots.present_when == "${HOME}/lab-run/plugins"
    assert "plugin-snapshots" in snapshots.argv
