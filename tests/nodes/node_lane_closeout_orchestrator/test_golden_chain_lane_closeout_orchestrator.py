# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane end over a fixture lane directory with two trees: inventory, guard verdicts, closeout."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.models.lane_closeout.enum_lane_guard_verdict import (
    EnumLaneGuardVerdict as Verdict,
)
from omnimarket.models.lane_closeout.model_lane_closeout_run_request import (
    ModelLaneCloseoutRunRequest,
)
from omnimarket.nodes.node_lane_closeout_orchestrator.handlers.handler_lane_closeout_run import (
    HandlerLaneCloseoutRun,
)
from tests.nodes.lane_closeout_support import (
    PROFILE_PARAMS,
    make_lane_dir,
    run_over_profile,
    wip_branches,
)

pytestmark = pytest.mark.unit


def test_the_lane_end_inventories_guards_and_closes_both_trees(tmp_path: Path) -> None:
    origin, lane_dir, clean, ahead = make_lane_dir(tmp_path)
    request = ModelLaneCloseoutRunRequest(
        lane="lane-1", lane_dir=str(lane_dir), run_id="r1"
    )
    result = HandlerLaneCloseoutRun().handle(request)
    assert result.correlation_id == request.correlation_id
    assert len(result.inventory.trees) == 2
    verdicts = {c.tree: c for c in result.closeouts}
    assert verdicts[str(clean)].verdict is Verdict.OK
    assert verdicts[str(ahead)].verdict is Verdict.WIP
    assert verdicts[str(ahead)].wip_push == "wip/lane-1-r1-OMN-1-beta"
    assert wip_branches(origin) == ["wip/lane-1-r1-OMN-1-beta"]
    assert not clean.exists()
    assert not ahead.exists()
    assert f"worktree=removed:{clean}" in result.summary
    assert "wip-pushed=wip/lane-1-r1-OMN-1-beta" in result.summary


def test_a_dirty_tree_is_kept_and_named_in_the_summary(tmp_path: Path) -> None:
    _, lane_dir, clean, ahead = make_lane_dir(tmp_path)
    (ahead / "new.py").write_text("x = 1\n", encoding="utf-8")
    result = HandlerLaneCloseoutRun().handle(
        ModelLaneCloseoutRunRequest(lane="lane-1", lane_dir=str(lane_dir))
    )
    assert ahead.exists()
    assert not clean.exists()
    assert f"worktree=kept:{ahead}:uncommitted-changes" in result.summary
    kept = [c for c in result.closeouts if c.verdict is Verdict.KEEP]
    assert len(kept) == 1
    assert kept[0].removed is False


def test_a_lane_that_made_no_trees_says_so(tmp_path: Path) -> None:
    result = HandlerLaneCloseoutRun().handle(
        ModelLaneCloseoutRunRequest(lane="lane-1", lane_dir=str(tmp_path / "none"))
    )
    assert result.closeouts == ()
    assert result.summary == "lane closeout: the lane made no worktrees"


@pytest.mark.parametrize("profile", PROFILE_PARAMS)
async def test_lane_end_over_the_bus_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    origin, lane_dir, _clean, _ahead = make_lane_dir(tmp_path)
    request = ModelLaneCloseoutRunRequest(
        lane="lane-1", lane_dir=str(lane_dir), run_id="r1"
    )
    runtime = await run_over_profile(
        "node_lane_closeout_orchestrator", request, profile, tmp_path, monkeypatch
    )
    result = runtime.handler_result
    assert result.correlation_id == request.correlation_id
    assert [c.verdict for c in result.closeouts].count(Verdict.OK) == 1
    assert [c.verdict for c in result.closeouts].count(Verdict.WIP) == 1
    assert wip_branches(origin) == ["wip/lane-1-r1-OMN-1-beta"]
    assert runtime._contract["terminal_event"] == (
        "onex.evt.omnimarket.lane-closeout-run-completed.v1"
    )
