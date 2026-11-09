# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Guard, WIP push and closeout of one tree: the OMN-20281 guard cases and the closeout cell."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.lane_closeout.enum_lane_guard_verdict import (
    EnumLaneGuardVerdict as Verdict,
)
from omnimarket.models.lane_closeout.model_lane_closeout_request import (
    ModelLaneCloseoutRequest,
)
from omnimarket.models.lane_closeout.model_lane_closeout_result import (
    ModelLaneCloseoutResult,
)
from omnimarket.nodes.node_lane_closeout_effect.handlers.handler_lane_closeout import (
    HandlerLaneCloseout,
)
from tests.nodes.lane_closeout_support import (
    PROFILE_PARAMS,
    add_worktree,
    clone,
    commit,
    git,
    make_lane_dir,
    make_origin,
    run_over_profile,
    wip_branches,
)

pytestmark = pytest.mark.unit


def close(
    tree: Path,
    *,
    tag: str = "lane-1",
    base: str = "",
    url: str = "",
    remove: bool = True,
) -> ModelLaneCloseoutResult:
    request = ModelLaneCloseoutRequest(
        lane="lane-1", tree=str(tree), tag=tag, base=base, url=url, remove=remove
    )
    return HandlerLaneCloseout().handle(request)


def test_a_clean_tree_that_matches_its_upstream_may_go(tmp_path: Path) -> None:
    tree = clone(tmp_path, make_origin(tmp_path))
    got = close(tree, remove=False)
    assert got.verdict is Verdict.OK
    assert got.wip_push == ""
    assert got.removed is False


def test_a_modified_tracked_file_keeps_the_tree(tmp_path: Path) -> None:
    origin = make_origin(tmp_path)
    tree = clone(tmp_path, origin)
    (tree / "f.txt").write_text("changed\n", encoding="utf-8")
    got = close(tree)
    assert (got.verdict, got.detail) == (Verdict.KEEP, "uncommitted-changes")
    assert wip_branches(origin) == []
    assert tree.exists()
    assert got.removed is False
    assert got.cell == f"worktree=kept:{tree}:uncommitted-changes"


def test_an_untracked_file_keeps_the_tree(tmp_path: Path) -> None:
    tree = clone(tmp_path, make_origin(tmp_path))
    (tree / "tests_new.py").write_text("x = 1\n", encoding="utf-8")
    got = close(tree)
    assert (got.verdict, got.detail) == (Verdict.KEEP, "uncommitted-changes")


def test_unpushed_commits_go_to_a_wip_branch_and_then_the_tree_may_go(
    tmp_path: Path,
) -> None:
    origin = make_origin(tmp_path)
    tree = clone(tmp_path, origin)
    commit(tree)
    head = git(tree, "rev-parse", "HEAD")
    got = close(tree, remove=False)
    assert (got.verdict, got.detail, got.wip_push) == (
        Verdict.WIP,
        "wip/lane-1",
        "wip/lane-1",
    )
    assert wip_branches(origin) == ["wip/lane-1"]
    assert git(origin, "rev-parse", "wip/lane-1") == head


def test_a_branch_with_no_upstream_is_never_ok(tmp_path: Path) -> None:
    origin = make_origin(tmp_path)
    tree = clone(tmp_path, origin)
    git(tree, "checkout", "-q", "-b", "feature/x")
    commit(tree)
    got = close(tree, remove=False)
    assert got.verdict is Verdict.WIP
    assert wip_branches(origin) == ["wip/lane-1"]


def test_a_branch_with_no_upstream_and_no_new_commit_is_still_pushed_not_dropped(
    tmp_path: Path,
) -> None:
    tree = clone(tmp_path, make_origin(tmp_path))
    git(tree, "checkout", "-q", "-b", "feature/empty")
    got = close(tree, remove=False)
    assert (got.verdict, got.detail) == (Verdict.WIP, "wip/lane-1")


def test_a_failed_wip_push_keeps_the_tree(tmp_path: Path) -> None:
    tree = clone(tmp_path, make_origin(tmp_path))
    commit(tree)
    git(tree, "remote", "set-url", "origin", str(tmp_path / "no-such-remote.git"))
    got = close(tree)
    assert got.verdict is Verdict.KEEP
    assert "push-failed" in got.detail
    assert tree.exists()


def test_a_detached_worktree_ahead_of_its_base_is_pushed_to_the_given_url(
    tmp_path: Path,
) -> None:
    origin = make_origin(tmp_path)
    tree = clone(tmp_path, origin)
    base = git(tree, "rev-parse", "HEAD")
    git(tree, "checkout", "-q", "--detach")
    git(tree, "remote", "remove", "origin")
    commit(tree)
    got = close(tree, base=base, url=str(origin), remove=False)
    assert (got.verdict, got.detail) == (Verdict.WIP, "wip/lane-1")
    assert wip_branches(origin) == ["wip/lane-1"]


def test_a_detached_worktree_at_its_base_may_go(tmp_path: Path) -> None:
    origin = make_origin(tmp_path)
    tree = clone(tmp_path, origin)
    base = git(tree, "rev-parse", "HEAD")
    git(tree, "checkout", "-q", "--detach")
    git(tree, "remote", "remove", "origin")
    got = close(tree, base=base, url=str(origin), remove=False)
    assert got.verdict is Verdict.OK


def linked(tmp_path: Path, origin: Path) -> Path:
    return add_worktree(
        clone(tmp_path, origin, tmp_path / "canon"), tmp_path / "wt" / "tree", "lane-x"
    )


def test_a_standalone_clone_is_never_removed(tmp_path: Path) -> None:
    tree = clone(tmp_path, make_origin(tmp_path))
    got = close(tree)
    assert got.verdict is Verdict.OK
    assert got.removed is False
    assert got.cell == f"worktree=kept:{tree}:standalone-clone"
    assert tree.exists()


def test_a_safe_tree_is_removed_and_the_cell_says_so(tmp_path: Path) -> None:
    tree = linked(tmp_path, make_origin(tmp_path))
    got = close(tree)
    assert got.verdict is Verdict.OK
    assert got.removed is True
    assert not tree.exists()
    assert got.cell == f"worktree=removed:{tree}"


def test_a_tree_with_unpushed_commits_is_removed_only_after_the_wip_push(
    tmp_path: Path,
) -> None:
    origin = make_origin(tmp_path)
    tree = linked(tmp_path, origin)
    commit(tree)
    got = close(tree)
    assert got.verdict is Verdict.WIP
    assert got.removed is True
    assert wip_branches(origin) == ["wip/lane-1"]
    assert not tree.exists()


@pytest.mark.parametrize("profile", PROFILE_PARAMS)
async def test_closeout_over_the_bus_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    origin, _, _, ahead = make_lane_dir(tmp_path)
    request = ModelLaneCloseoutRequest(
        lane="lane-1", tree=str(ahead), tag="lane-1-beta"
    )
    runtime = await run_over_profile(
        "node_lane_closeout_effect", request, profile, tmp_path, monkeypatch
    )
    result = runtime.handler_result
    assert result.correlation_id == request.correlation_id
    assert result.verdict is Verdict.WIP
    assert result.removed is True
    assert wip_branches(origin) == ["wip/lane-1-beta"]
    assert runtime._contract["terminal_event"] == (
        "onex.evt.omnimarket.lane-closeout-completed.v1"
    )


def test_uncommitted_changes_are_salvaged_to_the_named_branch_and_the_tree_may_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "t")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "t@example.invalid")
    origin = make_origin(tmp_path)
    tree = linked(tmp_path, origin)
    (tree / "f.txt").write_text("changed\n", encoding="utf-8")
    (tree / "new.py").write_text("x = 1\n", encoding="utf-8")
    request = ModelLaneCloseoutRequest(
        lane="lane-1", tree=str(tree), tag="lane-1", salvage_branch="salvage/lane-1"
    )
    got = HandlerLaneCloseout().handle(request)
    assert (got.verdict, got.detail, got.removed) == (
        Verdict.WIP,
        "salvage/lane-1",
        True,
    )
    assert git(origin, "show", "salvage/lane-1:f.txt") == "changed"
    assert git(origin, "show", "salvage/lane-1:new.py") == "x = 1"
    assert not tree.exists()


def test_a_salvage_that_cannot_push_keeps_the_dirty_tree_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "t")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "t@example.invalid")
    tree = linked(tmp_path, make_origin(tmp_path))
    (tree / "f.txt").write_text("changed\n", encoding="utf-8")
    git(tree, "remote", "set-url", "origin", str(tmp_path / "no-such-remote.git"))
    request = ModelLaneCloseoutRequest(
        lane="lane-1", tree=str(tree), tag="lane-1", salvage_branch="salvage/lane-1"
    )
    got = HandlerLaneCloseout().handle(request)
    assert (got.verdict, got.detail, got.removed) == (
        Verdict.KEEP,
        "salvage-push-failed",
        False,
    )
    assert (tree / "f.txt").read_text(encoding="utf-8") == "changed\n"
    assert git(tree, "status", "--porcelain") == "M f.txt"


def refusing_pre_push_hook(tree: Path) -> Path:
    """Install a pre-push hook that refuses every push, in the hook directory the tree uses."""
    common = Path(git(tree, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    hook = common / "hooks" / "pre-push"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    return hook


def test_a_salvage_push_does_not_run_the_checkout_pre_push_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "t")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "t@example.invalid")
    origin = make_origin(tmp_path)
    tree = linked(tmp_path, origin)
    refusing_pre_push_hook(tree)
    (tree / "f.txt").write_text("changed\n", encoding="utf-8")
    request = ModelLaneCloseoutRequest(
        lane="lane-1", tree=str(tree), tag="lane-1", salvage_branch="salvage/lane-1"
    )
    got = HandlerLaneCloseout().handle(request)
    assert (got.verdict, got.detail, got.removed) == (
        Verdict.WIP,
        "salvage/lane-1",
        True,
    )
    assert git(origin, "show", "salvage/lane-1:f.txt") == "changed"
    assert not tree.exists()


def test_a_wip_push_does_not_run_the_checkout_pre_push_hook(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{key}_NAME", "t")
        monkeypatch.setenv(f"GIT_{key}_EMAIL", "t@example.invalid")
    origin = make_origin(tmp_path)
    tree = linked(tmp_path, origin)
    refusing_pre_push_hook(tree)
    commit(tree)
    got = close(tree)
    assert (got.verdict, got.detail, got.removed) == (
        Verdict.WIP,
        "wip/lane-1",
        True,
    )
    assert wip_branches(origin) == ["wip/lane-1"]
