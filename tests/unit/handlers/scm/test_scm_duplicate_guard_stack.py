# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``onex scm pr-duplicate-check`` on a branch stacked on open pull requests, against real git checkouts.

The topology is the one recorded on 2026-11-01 for ``omn-21733-floor-dashboard-plain``: two open pull requests
in a chain (omnimarket#4727 then #4730, here #2159 then #2200), and a branch with a commit of its own on the
first one that then merges the second (c4433d697 merged a441104d3). The lane's local branch is behind the
pushed one, as the canonical clone's was (it held e36671962, without the merge). The commit subjects are the
recorded ones. Nothing is mocked; the overlay roots are pointed at an empty directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import Result

from omnimarket.handlers.scm import scm_cli
from omnimarket.models.model_pr_duplicate_guard import ModelPrDuplicateGuardRequest
from omnimarket.nodes.node_pr_duplicate_snapshot_effect.handlers.handler_pr_duplicate_snapshot import (
    load_stack_config,
    read_request,
)
from tests.unit.handlers.scm.test_scm_duplicate_guard import (
    BASELINE,
    LANE,
    NEW_BRANCH,
    TARGET,
    TITLE,
    World,
    commit_edit,
    git,
    invoke,
)

pytestmark = pytest.mark.unit

PARENT = "fix/omn-21533-protocol-binding-race"
DRAWER = "omn-21733-floor-overlay-drawer"
OWN_ONE = (
    "feat(OMN-21733): the dashboard opens with a plain verdict and what needs a person"
)
OWN_MERGE = "feat(OMN-21733): the dashboard carries the side panel for drill-downs in plain words"
DRAWER_SUBJECT = "feat(OMN-21733): open drill-downs in a side panel instead of jumping"


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    built = World(tmp_path)
    empty = tmp_path / "no-overlays"
    empty.mkdir()
    monkeypatch.setenv("ONEX_PR_WATCHER_STATE", str(built.state))
    monkeypatch.setenv("ONEX_LEDGER_PATH", str(built.ledger))
    monkeypatch.setenv("ONEX_SCM_CLONE_ROOT", str(built.root))
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(empty))
    monkeypatch.delenv("ONEX_WORKSPACE_CONFIG_ROOT", raising=False)
    return built


def build_stack(world: World) -> dict[str, str]:
    """#2159 (the parent), #2200 (its child), and the lane's branch: its own commit, then the merge of the child."""
    lane = world.lane
    git(lane, "checkout", "-q", "-b", DRAWER, f"origin/{PARENT}")
    drawer_tip = commit_edit(
        lane, "src/metrics.py", 5, "drawer change", message=DRAWER_SUBJECT
    )
    git(lane, "push", "-q", "origin", DRAWER)
    git(lane, "checkout", "-q", "-b", NEW_BRANCH, f"origin/{PARENT}")
    own_one = commit_edit(
        lane, "tests/test_binding.py", 71, "plain words", message=OWN_ONE
    )
    git(lane, "push", "-q", "origin", NEW_BRANCH)
    git(lane, "merge", "-q", "--no-ff", DRAWER, "-m", OWN_MERGE)
    merge = git(lane, "rev-parse", "HEAD")
    git(lane, "push", "-q", "origin", NEW_BRANCH)
    # the lane's local branch is behind the pushed one, as the canonical clone's was
    git(lane, "update-ref", f"refs/heads/{NEW_BRANCH}", own_one)
    git(lane, "checkout", "-q", "--detach", "origin/dev")
    state = json.loads(world.state.read_text())
    template = state["prs"]["omnibase_core#2159"]["facts"]
    state["prs"]["omnibase_core#2200"] = {
        "facts": {
            **template,
            "number": 2200,
            "head_ref": DRAWER,
            "head_sha": drawer_tip,
            "title": "feat(OMN-21733): drill-downs open in a side panel",
        }
    }
    world.state.write_text(json.dumps(state))
    return {"drawer": drawer_tip, "own_one": own_one, "merge": merge}


def snapshot(world: World) -> ModelPrDuplicateGuardRequest:
    return read_request(
        repo=TARGET,
        head_ref=NEW_BRANCH,
        base="dev",
        title=TITLE,
        body="",
        lane=LANE,
        cause=None,
        watcher_state=world.state,
        ledger=world.ledger,
        clone=world.root / "omnibase_core",
        worktree=world.lane,
    )


def test_own_change_is_read_from_the_newest_contained_open_pull_request(
    world: World,
) -> None:
    shas = build_stack(world)

    request = snapshot(world)

    prs = {pr.number: pr for pr in request.open_prs}
    assert prs[2159].stacked
    assert prs[2200].stacked
    assert request.judged_base == shas["drawer"][:12]
    assert request.touch is not None
    assert [(f.path, f.spans) for f in request.touch.files] == [
        ("tests/test_binding.py", ((71, 71),))
    ]


def test_own_change_the_pushed_tip_wins_over_a_stale_local_branch(
    world: World,
) -> None:
    shas = build_stack(world)

    request = snapshot(world)

    assert request.judged_commits[0].sha == shas["merge"][:12]


def test_judged_commits_name_only_the_commits_beyond_the_contained_pull_requests(
    world: World,
) -> None:
    shas = build_stack(world)

    request = snapshot(world)

    assert [(c.sha, c.subject) for c in request.judged_commits] == [
        (shas["merge"][:12], OWN_MERGE),
        (shas["own_one"][:12], OWN_ONE),
    ]


def test_own_change_a_pull_request_on_a_contained_pull_requests_lines_does_not_refuse(
    world: World,
) -> None:
    build_stack(world)
    git(world.lane, "checkout", "-q", "-b", "fix/omn-21533-again", "origin/dev")
    sibling = commit_edit(world.lane, BASELINE, 9, "another rename of the baseline")
    git(world.lane, "push", "-q", "origin", "fix/omn-21533-again")
    git(world.lane, "checkout", "-q", "--detach", "origin/dev")
    state = json.loads(world.state.read_text())
    template = state["prs"]["omnibase_core#2159"]["facts"]
    state["prs"]["omnibase_core#2210"] = {
        "facts": {
            **template,
            "number": 2210,
            "head_ref": "fix/omn-21533-again",
            "head_sha": sibling,
            "title": "feat(OMN-21733): another rename",
        }
    }
    world.state.write_text(json.dumps(state))

    result = invoke(world.check_args())

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["state"] == "clear"


def test_judged_commits_stack_rule_comes_from_the_snapshot_contract() -> None:
    assert load_stack_config().base_rule.value == "newest_contained_open_pr_head"


def refused_world(world: World) -> Result:
    world.lane_branch(BASELINE, 9)
    return invoke(world.check_args())


def test_judged_commits_are_in_the_json_answer_and_the_stderr_message_of_a_refusal(
    world: World,
) -> None:
    result = refused_world(world)

    assert result.exit_code == scm_cli.EXIT_DUPLICATE, result.output
    answer = json.loads(result.stdout)
    assert [c["subject"] for c in answer["judged_commits"]] == ["lane rename"]
    sha = answer["judged_commits"][0]["sha"]
    assert len(sha) == 12
    assert f"{sha} lane rename" in result.stderr
    assert "Judged 1 commit" in result.stderr


def test_judged_commits_are_named_on_stderr_when_the_answer_is_clear(
    world: World,
) -> None:
    world.lane_branch("src/metrics.py", 15)

    result = invoke(world.check_args())

    assert result.exit_code == 0, result.output
    answer = json.loads(result.stdout)
    assert [c["subject"] for c in answer["judged_commits"]] == ["lane rename"]
    assert "Judged 1 commit" in result.stderr
