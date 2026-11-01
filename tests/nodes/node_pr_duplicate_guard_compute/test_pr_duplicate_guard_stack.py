# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A stacked branch is judged on its own change, and the refusal names the commits it judged.

The recorded inputs are the refusals of 2026-11-01. ``onex scm pr-create`` refused the branch
``omn-21733-floor-dashboard-plain`` (head c4433d697), which holds the commits of omnimarket#4727 and #4730
plus two of its own, as a duplicate of #4727 and listed #4731 and #4733 as touching the same line
(``page/index.html:9``). The fixture files hold what the snapshot read at that tick: the diff spans and
changed-line digests of the four pull requests and the branch, and the branch's commit list. A second
fixture holds the Codex ``--cd`` fix branch and pull request #1366 of a private plugin repository (named example_plugins here), which rewrite one run-script line.
Every test builds its request in memory; no file or overlay is read.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.model_pr_duplicate_guard import (
    EnumDuplicateEvidence,
    ModelFileTouch,
    ModelJudgedCommit,
    ModelOpenPr,
    ModelPrDuplicateGuardRequest,
    ModelPrDuplicateGuardResult,
    ModelPrTouch,
)
from omnimarket.nodes.node_pr_duplicate_guard_compute.handlers import (
    handler_pr_duplicate_guard as handler_module,
)
from omnimarket.nodes.node_pr_duplicate_guard_compute.handlers.handler_pr_duplicate_guard import (
    HandlerPrDuplicateGuard,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 1, 0, 46, tzinfo=UTC)
INDEX = "src/omnimarket/nodes/node_factory_floor_publish_effect/page/index.html"
GUARD = "src/example_repo/nodes/node_cause_fix_guard/handler.py"
STACK_BASE_SUBJECT = (
    "feat(OMN-21733): open drill-downs in a side panel instead of jumping"
)


def recorded_stack() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (FIXTURES / "recorded_dashboard_stack_20261101.json").read_text()
    )
    return data


def recorded_codex() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(
        (FIXTURES / "recorded_codex_cd_1366_20261101.json").read_text()
    )
    return data


def judged_from_record(record: dict[str, Any]) -> tuple[ModelJudgedCommit, ...]:
    """The two commits the branch adds beyond the open pull requests it contains (the stack's own change)."""
    own = [
        c
        for c in record["branch_commits"]
        if c["sha"].startswith(("c4433d697", "e36671962"))
    ]
    return tuple(
        ModelJudgedCommit(sha=c["sha"][:12], subject=c["subject"]) for c in own
    )


def dashboard_request(
    *, title: str = "", judged: bool = True
) -> ModelPrDuplicateGuardRequest:
    record = recorded_stack()
    return ModelPrDuplicateGuardRequest.model_validate(
        {
            "repo": record["repo"],
            "head_ref": record["new_branch"]["head_ref"],
            "base": record["new_branch"]["base"],
            "title": title,
            "now": NOW,
            "touch": record["new_branch"]["touch"],
            "open_prs": [
                {k: v for k, v in pr.items() if k != "head_sha"}
                for pr in record["open_prs"]
            ],
            "judged_commits": judged_from_record(record) if judged else (),
            "judged_base": "a441104d340c" if judged else None,
        }
    )


def run(request: ModelPrDuplicateGuardRequest) -> ModelPrDuplicateGuardResult:
    return HandlerPrDuplicateGuard().handle(request)


# ---- the recorded stack of 2026-11-01


@pytest.mark.parametrize("title", ["", "feat(OMN-21850): the dashboard side panel"])
def test_recorded_stack_dashboard_branch_is_clear(title: str) -> None:
    result = run(dashboard_request(title=title))

    assert result.state == "clear", result.message
    assert result.evidence == ()
    assert result.adopt_pr is None


def test_recorded_stack_siblings_on_the_same_line_are_noted_not_refused() -> None:
    result = run(dashboard_request())

    noted = " ".join(result.notes)
    assert "omnimarket#4731" in noted
    assert "omnimarket#4733" in noted
    assert f"{INDEX}:9" in noted


def test_recorded_stack_contained_pull_requests_were_read_as_stacked() -> None:
    record = recorded_stack()
    stacked = {pr["number"]: pr["stacked"] for pr in record["open_prs"]}

    assert stacked == {4727: True, 4730: True, 4731: False, 4733: False}
    # the branch is a merge of the drawer branch (#4730) into its own commit on top of #4727
    parents = {
        c["sha"][:9]: c["parents"].count(" ") + 1 for c in record["branch_commits"]
    }
    assert parents["c4433d697"] == 2


def test_recorded_stack_the_branch_against_a_copy_of_itself_is_still_refused() -> None:
    record = recorded_stack()
    request = dashboard_request()
    twin = ModelOpenPr(
        number=4800,
        head_ref="someone/else-same-change",
        base="dev",
        title="feat(OMN-21733): the same dashboard change",
        touch=request.touch,
    )

    result = run(request.model_copy(update={"open_prs": (*request.open_prs, twin)}))

    assert record["new_branch"]["head_ref"] != twin.head_ref
    assert result.state == "adopt"
    assert "omnimarket#4800" in {e.pr for e in result.evidence}
    assert {e.kind for e in result.evidence} == {EnumDuplicateEvidence.LINES}


def test_recorded_stack_codex_cd_branch_beside_1366_is_clear() -> None:
    record = recorded_codex()
    pr = record["open_pr"]
    branch = record["codex_branch"]
    result = run(
        ModelPrDuplicateGuardRequest(
            repo=record["repo"],
            head_ref=branch["head_ref"],
            base="main",
            title="fix(OMN-21843): codex exec -C gets the lane worktree",
            now=NOW,
            touch=ModelPrTouch(
                files=(
                    ModelFileTouch(
                        path=branch["path"],
                        spans=tuple((a, b) for a, b in branch["spans"]),
                        digest=tuple(branch["digest"]),
                    ),
                )
            ),
            open_prs=(
                ModelOpenPr.model_validate(
                    {k: v for k, v in pr.items() if k != "head_sha"}
                ),
            ),
        )
    )

    assert result.state == "clear", result.message
    assert any("example_plugins#1366" in note for note in result.notes)


# ---- own_change: what the branch is judged on


def test_own_change_commits_of_contained_pull_requests_never_make_a_duplicate() -> None:
    """Both contained pull requests are stacked; the lines the branch shares with them are not its own."""
    shared = ModelFileTouch(
        path=GUARD, spans=((204, 204),), digest=("aaaaaaaaaaaa", "bbbbbbbbbbbb")
    )
    own = ModelFileTouch(path=GUARD, spans=((300, 300),), digest=("cccccccccccc",))
    contained = [
        ModelOpenPr(
            number=number,
            head_ref=f"fix/omn-7-part-{number}",
            base="dev",
            title=f"fix(OMN-7): part {number}",
            touch=ModelPrTouch(files=(shared,)),
            stacked=True,
        )
        for number in (1, 2)
    ]

    result = run(
        ModelPrDuplicateGuardRequest(
            repo="example_repo",
            head_ref="fix/omn-7-part-3",
            base="dev",
            title="fix(OMN-7): part 3",
            now=NOW,
            touch=ModelPrTouch(files=(own,)),
            open_prs=tuple(contained),
        )
    )

    assert result.state == "clear"
    assert result.evidence == ()


def test_own_change_a_stacked_pull_request_is_skipped_even_with_the_branch_diff_on_its_lines() -> (
    None
):
    """A diff read from the wrong base (the stack's commits included) must not turn a contained pull request into a duplicate."""
    lines = ModelFileTouch(path=GUARD, spans=((204, 204),), digest=("aaaaaaaaaaaa",))
    result = run(
        ModelPrDuplicateGuardRequest(
            repo="example_repo",
            head_ref="fix/omn-7-part-2",
            base="dev",
            title="fix(OMN-7): part 2",
            now=NOW,
            touch=ModelPrTouch(files=(lines,)),
            open_prs=(
                ModelOpenPr(
                    number=1,
                    head_ref="fix/omn-7-part-1",
                    base="dev",
                    title="fix(OMN-7): part 1",
                    touch=ModelPrTouch(files=(lines,)),
                    stacked=True,
                ),
            ),
        )
    )

    assert result.state == "clear"


# ---- intent and same_change: same lines, different change


def edit(spans: tuple[tuple[int, int], ...], digest: tuple[str, ...]) -> ModelPrTouch:
    return ModelPrTouch(files=(ModelFileTouch(path=GUARD, spans=spans, digest=digest),))


def pair(
    *,
    mine_title: str,
    theirs_title: str,
    mine: tuple[str, ...],
    theirs: tuple[str, ...],
) -> ModelPrDuplicateGuardResult:
    return run(
        ModelPrDuplicateGuardRequest(
            repo="example_repo",
            head_ref="feature/new-branch",
            base="dev",
            title=mine_title,
            now=NOW,
            touch=edit(((204, 204),), mine),
            open_prs=(
                ModelOpenPr(
                    number=9,
                    head_ref="feature/old-branch",
                    base="dev",
                    title=theirs_title,
                    touch=edit(((204, 204),), theirs),
                ),
            ),
        )
    )


DIGEST_A = tuple(f"{n:012d}" for n in range(10))
DIGEST_OTHER = tuple(f"{n:012d}" for n in range(100, 110))


def test_intent_other_ticket_and_other_change_on_the_same_line_is_clear() -> None:
    result = pair(
        mine_title="feat(OMN-1): new",
        theirs_title="feat(OMN-2): old",
        mine=DIGEST_A,
        theirs=DIGEST_OTHER,
    )

    assert result.state == "clear"
    assert "different intent" in result.notes[0]


def test_intent_one_ticket_over_many_pull_requests_is_not_one_change() -> None:
    """An umbrella ticket: the same ticket and the same line, but changed lines that share almost nothing."""
    result = pair(
        mine_title="feat(OMN-1): new",
        theirs_title="feat(OMN-1): old",
        mine=DIGEST_A,
        theirs=DIGEST_OTHER,
    )

    assert result.state == "clear"
    assert "same ticket" in result.notes[0]


def test_same_change_same_ticket_is_still_refused() -> None:
    result = pair(
        mine_title="feat(OMN-1): new",
        theirs_title="feat(OMN-1): old",
        mine=DIGEST_A,
        theirs=DIGEST_A[:8] + DIGEST_OTHER[:2],
    )

    assert result.state == "adopt"
    assert result.adopt_pr == "example_repo#9"
    assert result.evidence[0].kind is EnumDuplicateEvidence.LINES


def test_same_change_without_a_shared_ticket_is_still_refused() -> None:
    result = pair(
        mine_title="feat(OMN-1): new",
        theirs_title="feat(OMN-2): old",
        mine=DIGEST_A,
        theirs=DIGEST_A,
    )

    assert result.state == "adopt"
    assert result.evidence[0].kind is EnumDuplicateEvidence.LINES


def test_same_change_a_shared_ticket_with_unreadable_digests_is_still_refused() -> None:
    result = pair(
        mine_title="feat(OMN-1): new",
        theirs_title="feat(OMN-1): old",
        mine=(),
        theirs=(),
    )

    assert result.state == "adopt"


# ---- judged_commits: the answer names what it judged


def test_judged_commits_are_named_in_a_clear_answer() -> None:
    result = run(dashboard_request())
    answer = result.model_dump(mode="json")

    assert [c["subject"] for c in answer["judged_commits"]] == [
        c.subject for c in judged_from_record(recorded_stack())
    ]
    assert answer["judged_base"] == "a441104d340c"
    assert "c4433d697004" in result.message
    assert "e36671962b71" in result.message


def test_judged_commits_are_named_in_a_refusal() -> None:
    request = dashboard_request()
    twin = ModelOpenPr(
        number=4800,
        head_ref="someone/else-same-change",
        base="dev",
        title="feat(OMN-21733): the same dashboard change",
        touch=request.touch,
    )

    result = run(request.model_copy(update={"open_prs": (*request.open_prs, twin)}))

    assert result.state == "adopt"
    assert [c.sha for c in result.judged_commits] == ["c4433d697004", "e36671962b71"]
    assert "Judged 2 commits" in result.message
    assert "e36671962b71" in result.message
    assert "a441104d340c" in result.message


def test_judged_commits_listed_in_the_message_are_capped_by_the_contract() -> None:
    shown = handler_module.load_config().report.judged_commits_shown
    commits = tuple(
        ModelJudgedCommit(sha=f"{n:012x}", subject=f"commit {n}")
        for n in range(shown + 3)
    )
    request = dashboard_request().model_copy(update={"judged_commits": commits})

    result = run(request)

    assert len(result.judged_commits) == shown + 3
    assert f"{shown:012x}" not in result.message
    assert "3 more" in result.message


def test_judged_commits_none_read_says_so() -> None:
    result = run(dashboard_request(judged=False))

    assert result.judged_commits == ()
    assert "no commits read" in result.message


# ---- AC4: declared data, read through typed models


def test_own_change_stack_and_intent_rules_are_declared_in_the_contracts() -> None:
    guard = yaml.safe_load((handler_module.NODE_ROOT / "contract.yaml").read_text())
    assert guard["config"]["intent"] == {
        "ticket_pattern": "OMN-[0-9]+",
        "min_same_change_similarity": 0.6,
        "min_same_ticket_similarity": 0.1,
    }
    assert guard["config"]["report"] == {"judged_commits_shown": 12}
    snapshot = yaml.safe_load(
        (
            handler_module.NODE_ROOT.parent
            / "node_pr_duplicate_snapshot_effect"
            / "contract.yaml"
        ).read_text()
    )
    assert snapshot["config"]["stack"] == {"base_rule": "newest_contained_open_pr_head"}
    config = handler_module.load_config()
    assert (
        config.intent.min_same_ticket_similarity
        < config.intent.min_same_change_similarity
    )


def test_own_change_handler_sources_hold_none_of_the_declared_values() -> None:
    guard_source = Path(handler_module.__file__).read_text()
    snapshot_source = (
        Path(handler_module.__file__).parents[2]
        / "node_pr_duplicate_snapshot_effect"
        / "handlers"
        / "handler_pr_duplicate_snapshot.py"
    ).read_text()
    assert "0.1" not in guard_source
    assert "newest_contained_open_pr_head" not in snapshot_source
