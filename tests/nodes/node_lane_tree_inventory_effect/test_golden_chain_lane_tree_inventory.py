# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane tree inventory: one line per tree (repository, branch, HEAD, clean, unpushed, upstream)."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.lane_closeout.model_lane_tree_inventory_request import (
    ModelLaneTreeInventoryRequest,
)
from omnimarket.nodes.node_lane_tree_inventory_effect.handlers.handler_lane_tree_inventory import (
    HandlerLaneTreeInventory,
)
from tests.nodes.lane_closeout_support import (
    PROFILE_PARAMS,
    git,
    make_lane_dir,
    run_over_profile,
)

pytestmark = pytest.mark.unit


def test_two_trees_each_get_one_line(tmp_path: Path) -> None:
    _, lane_dir, clean, ahead = make_lane_dir(tmp_path)
    result = HandlerLaneTreeInventory().handle(
        ModelLaneTreeInventoryRequest(lane_dir=str(lane_dir))
    )
    by_path = {t.path: t for t in result.trees}
    assert sorted(by_path) == sorted([str(clean), str(ahead)])
    a, b = by_path[str(clean)], by_path[str(ahead)]
    assert (a.repository, a.branch, a.clean, a.unpushed) == (
        "alpha",
        "lane-alpha",
        True,
        0,
    )
    assert a.upstream == "origin/main"
    assert a.head == git(clean, "rev-parse", "HEAD")
    assert (b.repository, b.branch, b.clean, b.unpushed) == (
        "beta",
        "lane-beta",
        True,
        1,
    )
    assert len(result.lines) == 2
    assert result.lines[0] == (
        f"repository=alpha branch=lane-alpha head={a.head} clean=true unpushed=0 upstream=origin/main"
    )


def test_an_untracked_file_is_not_clean_and_a_detached_head_has_no_branch(
    tmp_path: Path,
) -> None:
    _, lane_dir, clean, ahead = make_lane_dir(tmp_path)
    (clean / "new.py").write_text("x = 1\n", encoding="utf-8")
    git(ahead, "checkout", "-q", "--detach")
    result = HandlerLaneTreeInventory().handle(
        ModelLaneTreeInventoryRequest(lane_dir=str(lane_dir))
    )
    by_path = {t.path: t for t in result.trees}
    assert by_path[str(clean)].clean is False
    assert by_path[str(ahead)].branch == ""
    assert by_path[str(ahead)].upstream == ""
    assert "branch=- " in result.lines[1]
    assert "upstream=-" in result.lines[1]


def test_a_nested_git_directory_is_not_another_tree(tmp_path: Path) -> None:
    _, lane_dir, clean, _ = make_lane_dir(tmp_path)
    git(clean, "init", "-q", str(clean / "vendor" / "inner"))
    result = HandlerLaneTreeInventory().handle(
        ModelLaneTreeInventoryRequest(lane_dir=str(lane_dir))
    )
    assert len(result.trees) == 2


def test_a_missing_lane_directory_lists_nothing(tmp_path: Path) -> None:
    result = HandlerLaneTreeInventory().handle(
        ModelLaneTreeInventoryRequest(lane_dir=str(tmp_path / "absent"))
    )
    assert result.trees == ()
    assert result.lines == ()


@pytest.mark.parametrize("profile", PROFILE_PARAMS)
async def test_inventory_over_the_bus_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    _, lane_dir, _, _ = make_lane_dir(tmp_path)
    request = ModelLaneTreeInventoryRequest(lane_dir=str(lane_dir))
    runtime = await run_over_profile(
        "node_lane_tree_inventory_effect", request, profile, tmp_path, monkeypatch
    )
    result = runtime.handler_result
    assert result.correlation_id == request.correlation_id
    assert len(result.trees) == 2
    assert runtime._contract["terminal_event"] == (
        "onex.evt.omnimarket.lane-tree-inventory-completed.v1"
    )
