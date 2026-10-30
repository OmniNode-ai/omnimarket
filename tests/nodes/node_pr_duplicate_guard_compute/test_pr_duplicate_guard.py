# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One fix, one pull request: the fold against the two planted cases of 2026-10-30 (OMN-17427).

The numbers are the real diffs. omnibase_core#2159, #2160 and #2161 each replaced line 9 of
``.onex_ratchets/protocol_binding_baseline.txt`` and line 70 of the baseline test. In a second repository
(``example_repo`` here) #1320 and #1326 both rewrote ``dispatch_fix_guard`` in the fix guard's handler
(old lines 204 and 211) and the landing worker. Each lane's brief named the cause and nothing checked for
the open pull request.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.models.model_pr_duplicate_guard import (
    EnumDuplicateEvidence,
    ModelFileTouch,
    ModelOpenPr,
    ModelPrDuplicateGuardRequest,
    ModelPrDuplicateGuardResult,
    ModelPrTouch,
)
from omnimarket.nodes.node_pr_duplicate_guard_compute.handlers.handler_pr_duplicate_guard import (
    HandlerPrDuplicateGuard,
    distinctive_words,
    overlap,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 30, 0, 40, tzinfo=UTC)
BASELINE = ".onex_ratchets/protocol_binding_baseline.txt"
BASELINE_TEST = "tests/unit/validation/test_checker_protocol_binding.py"
GUARD = "src/example_repo/nodes/node_cause_fix_guard/handler.py"
WORKER = "plugins/skills/merge/scripts/landing_worker_run.py"


def touch(*files: ModelFileTouch) -> ModelPrTouch:
    return ModelPrTouch(files=files)


def edit(path: str, *spans: tuple[int, int]) -> ModelFileTouch:
    return ModelFileTouch(path=path, spans=spans)


def run(**fields: object) -> ModelPrDuplicateGuardResult:
    return HandlerPrDuplicateGuard().handle(
        ModelPrDuplicateGuardRequest.model_validate({"now": NOW, **fields})
    )


# ---- omnibase_core: three pull requests, one baseline line

RENAME = touch(edit(BASELINE, (9, 9)), edit(BASELINE_TEST, (70, 70)))
CORE_2159 = ModelOpenPr(
    number=2159,
    head_ref="fix/omn-21533-protocol-binding-race",
    base="dev",
    title="OMN-21533: follow the ci_inheritance runtime move in the protocol_binding baseline",
    touch=touch(
        edit(BASELINE, (9, 9)),
        edit(BASELINE_TEST, (70, 70)),
        ModelFileTouch(path="contracts/OMN-21533.yaml"),
        ModelFileTouch(
            path="tests/unit/validation/test_protocol_binding_baseline_names_live_files.py",
            added=True,
        ),
    ),
)
CORE_2160 = ModelOpenPr(
    number=2160,
    head_ref="jake/fix-dev-k5-baseline-moved-path",
    base="dev",
    title="OMN-21557: K5 protocol_binding baseline follows the CI-inheritance runtime to its node path (dev red)",
    touch=RENAME,
)


def test_the_second_baseline_rename_is_refused_with_the_first_named() -> None:
    result = run(
        repo="OmniNode-ai/omnibase_core",
        head_ref="OMN-21518-protocol-binding-baseline-rename",
        base="dev",
        title="fix(OMN-21518): protocol_binding baseline and test name the file that now holds the CI inheritance construction",
        lane="core-dev-protocol-baseline-0030",
        touch=RENAME,
        open_prs=(CORE_2159,),
    )

    assert result.state == "adopt"
    assert result.adopt_pr == "omnibase_core#2159"
    kinds = {e.kind for e in result.evidence}
    assert EnumDuplicateEvidence.LINES in kinds
    assert f"{BASELINE}:9" in result.evidence[0].detail
    assert "omnibase_core#2159" in result.message
    assert "adopted=omnibase_core#2159" in result.message


def test_the_third_baseline_rename_names_the_oldest_of_two_open_ones() -> None:
    result = run(
        repo="omnibase_core",
        head_ref="OMN-21518-protocol-binding-baseline-rename",
        base="dev",
        title="fix(OMN-21518): protocol_binding baseline and test name the file that now holds the CI inheritance construction",
        touch=RENAME,
        open_prs=(CORE_2160, CORE_2159),
    )

    assert result.state == "adopt"
    assert result.adopt_pr == "omnibase_core#2159"
    assert [e.pr for e in result.evidence] == [
        "omnibase_core#2159",
        "omnibase_core#2160",
    ]


# ---- example_repo: two pull requests, one dispatch

OIC_1320 = ModelOpenPr(
    number=1320,
    head_ref="omn-19998-perpr-fix-guard-dispatch",
    base="main",
    title="fix(OMN-19998): a per-PR worker on a lab host gets the controller's fix-guard read",
    touch=touch(
        edit(GUARD, (198, 198), (200, 204), (211, 211), (314, 314), (317, 320)),
        edit(WORKER, (701, 701), (745, 745), (2646, 2646)),
        edit("tests/test_cause_fix_guard.py", (335, 336)),
    ),
)
OIC_1326_TOUCH = touch(
    edit(GUARD, (204, 204), (211, 211)),
    edit(WORKER, (606, 606), (699, 699), (743, 743), (1931, 1931), (2638, 2638)),
    ModelFileTouch(
        path="plugins/skills/merge/tests/test_codex_inherits_lane_env.py",
        added=True,
    ),
)


def test_the_second_dispatch_fix_guard_is_refused_with_the_first_named() -> None:
    result = run(
        repo="OmniNode-ai/example_repo",
        head_ref="omn19998-codex-env-and-fixguard",
        base="main",
        title="fix(OMN-19998): a Codex worker on a lab host keeps the lane environment and reads the fix guard the controller ran",
        lane="codex-worker-blocked-0030",
        touch=OIC_1326_TOUCH,
        open_prs=(OIC_1320,),
    )

    assert result.state == "adopt"
    assert result.adopt_pr == "example_repo#1320"
    assert result.evidence[0].kind is EnumDuplicateEvidence.LINES
    assert (
        f"{GUARD}:204" in result.evidence[0].detail
    )  # old line 204 is in both rewrites


def test_same_file_and_three_shared_title_words_is_a_duplicate_without_a_shared_line() -> (
    None
):
    other = touch(edit(GUARD, (300, 301)))
    result = run(
        repo="example_repo",
        head_ref="b2",
        base="main",
        title="fix: per-PR worker on a lab host reads the controller's fix-guard",
        touch=other,
        open_prs=(OIC_1320,),
    )

    assert result.state == "adopt"
    assert result.evidence[0].kind is EnumDuplicateEvidence.FILES_AND_TITLE
    assert {"worker", "controller", "guard"} <= set(
        result.evidence[0].detail.replace(",", "").split()
    )


# ---- an unrelated pull request in the same repository passes


def test_an_unrelated_pull_request_in_the_same_repository_passes() -> None:
    result = run(
        repo="omnibase_core",
        head_ref="feat/omn-21600-event-bus-metrics",
        base="dev",
        title="feat(OMN-21600): event bus substrate publishes depth metrics",
        lane="metrics-lane",
        touch=touch(edit("src/omnibase_core/event_bus/metrics.py", (10, 12))),
        open_prs=(CORE_2159, CORE_2160),
    )

    assert result.state == "clear"
    assert result.adopt_pr is None
    assert result.evidence == ()


def test_the_same_file_on_other_lines_under_an_unrelated_title_passes_with_a_note() -> (
    None
):
    result = run(
        repo="omnibase_core",
        head_ref="feat/baseline-tail",
        base="dev",
        title="feat(OMN-21601): add a ratchet row for the metrics module",
        touch=touch(edit(BASELINE, (40, 40))),
        open_prs=(CORE_2159,),
    )

    assert result.state == "clear"
    assert any("omnibase_core#2159" in n and BASELINE in n for n in result.notes)


def test_two_pull_requests_appending_to_one_file_do_not_share_lines() -> None:
    appended = touch(ModelFileTouch(path="README.md"))
    other = ModelOpenPr(
        number=5, head_ref="x", base="dev", title="docs: a", touch=appended
    )
    result = run(
        repo="omnibase_core",
        head_ref="y",
        base="dev",
        title="docs: b",
        touch=appended,
        open_prs=(other,),
    )

    assert result.state == "clear"


def test_a_shared_lock_file_or_ticket_contract_says_nothing() -> None:
    generic = touch(
        edit("uv.lock", (1, 99)),
        ModelFileTouch(path="contracts/OMN-17427.yaml", spans=((4000, 4001),)),
    )
    other = ModelOpenPr(number=5, head_ref="x", base="dev", title="a", touch=generic)
    assert other.touch is not None
    lines, shared = overlap(generic, other.touch)

    assert lines == []
    assert shared == []


def test_a_pull_request_against_another_base_or_one_this_is_stacked_on_passes() -> None:
    backport = CORE_2160.model_copy(update={"base": "main"})
    stacked_on = CORE_2160.model_copy(update={"head_ref": "stack-base"})
    for candidate in (backport, stacked_on):
        result = run(
            repo="omnibase_core",
            head_ref="top",
            base="stack-base" if candidate is stacked_on else "dev",
            title="fix: something",
            touch=RENAME,
            open_prs=(candidate,),
        )
        assert result.state == "clear", candidate


def test_a_branch_that_already_has_an_open_pull_request_is_that_pull_request() -> None:
    result = run(
        repo="omnibase_core",
        head_ref=CORE_2160.head_ref,
        base="dev",
        title="anything",
        open_prs=(CORE_2160,),
    )

    assert result.state == "adopt"
    assert result.evidence[0].kind is EnumDuplicateEvidence.BRANCH
    assert result.adopt_pr == "omnibase_core#2160"


def test_an_open_pull_request_whose_diff_was_not_read_is_listed_not_guessed() -> None:
    unread = CORE_2159.model_copy(update={"touch": None})
    result = run(
        repo="omnibase_core",
        head_ref="z",
        base="dev",
        title="fix: unrelated words here",
        touch=RENAME,
        open_prs=(unread,),
    )

    assert result.state == "clear"
    assert result.unread == ("omnibase_core#2159",)


# ---- the cause cited, and the ledger CLAIM


def claim(
    lane: str, cause: str, stamp: str = "2026-10-30T00:20:00Z", **cells: str
) -> str:
    extra = "".join(f" | {k}={v}" for k, v in cells.items())
    return f"{stamp} | CLAIM | lane={lane} | principal=p | ticket=OMN-1 | cause={cause}{extra} | work\n"


def test_a_fix_title_citing_the_lanes_own_cause_check_is_the_same_cause() -> None:
    ledger = claim("me", "omnibase_core:Pre-commit Suite")
    existing = ModelOpenPr(
        number=2158,
        head_ref="h",
        base="dev",
        title="fix(OMN-18899): Pre-commit Suite stops failing check-protocol-binding",
        touch=touch(edit("scripts/other.py", (1, 1))),
    )
    result = run(
        repo="omnibase_core",
        head_ref="mine",
        base="dev",
        title="fix: the pre_commit suite",
        lane="me",
        touch=touch(edit("scripts/mine.py", (5, 5))),
        open_prs=(existing,),
        ledger_text=ledger,
    )

    assert result.state == "adopt"
    assert result.evidence[0].kind is EnumDuplicateEvidence.CAUSE_CITED
    assert result.adopt_pr == "omnibase_core#2158"


def test_a_fix_check_marker_in_both_bodies_is_the_same_cause() -> None:
    existing = ModelOpenPr(
        number=7,
        head_ref="h",
        base="dev",
        title="fix: first",
        body="text\n\nFix-Check: CI Summary\n",
        touch=touch(edit("a.py", (1, 1))),
    )
    result = run(
        repo="omnibase_core",
        head_ref="mine",
        base="dev",
        title="fix: second",
        body="Fix-Check: CI Summary (OMN-5)\n",
        touch=touch(edit("b.py", (1, 1))),
        open_prs=(existing,),
    )

    assert result.state == "adopt"
    assert result.adopt_pr == "omnibase_core#7"


def test_another_lanes_live_claim_with_the_same_cause_cell_owns_the_fix() -> None:
    ledger = claim(
        "cause-lane",
        "omnibase_core:repo-evidence / dod-verify",
        pr="omnibase_core#2159",
    ) + claim("me", "omnibase_core:Repo-Evidence / DoD_Verify", "2026-10-30T00:30:00Z")
    result = run(
        repo="omnibase_core",
        head_ref="mine",
        base="dev",
        title="fix: evidence",
        lane="me",
        touch=touch(edit("b.py", (1, 1))),
        open_prs=(CORE_2159,),
        ledger_text=ledger,
    )

    assert result.state == "adopt"
    claim_evidence = [
        e for e in result.evidence if e.kind is EnumDuplicateEvidence.CLAIM
    ]
    assert claim_evidence[0].lane == "cause-lane"
    assert result.adopt_pr == "omnibase_core#2159"
    assert result.adopt_lane == "cause-lane"


def test_a_claim_naming_no_open_pull_request_still_names_its_lane() -> None:
    ledger = claim("cause-lane", "omnibase_core:CI Summary") + claim(
        "me", "omnibase_core:CI Summary", "2026-10-30T00:30:00Z"
    )
    result = run(repo="omnibase_core", head_ref="mine", lane="me", ledger_text=ledger)

    assert result.state == "adopt"
    assert result.adopt_pr is None
    assert result.adopt_lane == "cause-lane"
    assert "cause-lane" in result.message


@pytest.mark.parametrize(
    "ledger",
    [
        claim(
            "me", "omnibase_core:CI Summary"
        ),  # the lane's own claim is not a duplicate
        claim("other", "omnibase_core:CI Summary")
        + claim("me", "omnibase_core:Other Check"),
        claim("other", "omnibase_infra:CI Summary")
        + claim("me", "omnibase_core:CI Summary"),  # another repo
        claim("other", "omnibase_core:CI Summary")
        + "2026-10-30T00:25:00Z | TERMINAL | lane=other | outcome=done\n"
        + claim("me", "omnibase_core:CI Summary", "2026-10-30T00:30:00Z"),  # released
        claim("other", "omnibase_core:CI Summary", "2026-10-29T22:00:00Z")
        + claim(
            "me", "omnibase_core:CI Summary", "2026-10-30T00:30:00Z"
        ),  # silent for hours
    ],
)
def test_claims_that_do_not_own_the_cause_do_not_block(ledger: str) -> None:
    result = run(repo="omnibase_core", head_ref="mine", lane="me", ledger_text=ledger)

    assert result.state == "clear"


def test_distinctive_words_drop_tickets_and_stop_words() -> None:
    assert distinctive_words(
        "fix(OMN-19998): the check wrongly names the controller guard"
    ) == {
        "controller",
        "guard",
    }
