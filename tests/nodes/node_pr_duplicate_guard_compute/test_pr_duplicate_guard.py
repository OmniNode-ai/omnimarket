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
from pathlib import Path

import pytest
import yaml

from omnimarket.models.model_pr_duplicate_guard import (
    EnumDuplicateEvidence,
    ModelDuplicateGuardRules,
    ModelFileTouch,
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
    distinctive_words,
    load_generic_paths,
    overlap,
)

pytestmark = pytest.mark.unit

GENERIC = ModelDuplicateGuardRules(generic_paths=load_generic_paths())
NOW = datetime(2026, 10, 30, 0, 40, tzinfo=UTC)
BASELINE = ".onex_ratchets/protocol_binding_baseline.txt"
BASELINE_TEST = "tests/unit/validation/test_checker_protocol_binding.py"
GUARD = "src/example_repo/nodes/node_cause_fix_guard/handler.py"
WORKER = "plugins/skills/merge/scripts/landing_worker_run.py"


def touch(*files: ModelFileTouch) -> ModelPrTouch:
    return ModelPrTouch(files=files)


def edit(
    path: str, *spans: tuple[int, int], digest: tuple[str, ...] = ()
) -> ModelFileTouch:
    return ModelFileTouch(path=path, spans=spans, digest=digest)


def run(**fields: object) -> ModelPrDuplicateGuardResult:
    return HandlerPrDuplicateGuard().handle(
        ModelPrDuplicateGuardRequest.model_validate({"now": NOW, **fields})
    )


# ---- omnibase_core: three pull requests, one baseline line

RENAME = touch(
    edit(BASELINE, (9, 9), digest=("abc123abc123",)),
    edit(BASELINE_TEST, (70, 70), digest=("def456def456",)),
)
CORE_2159 = ModelOpenPr(
    number=2159,
    head_ref="fix/omn-21533-protocol-binding-race",
    base="dev",
    title="OMN-21533: follow the ci_inheritance runtime move in the protocol_binding baseline",
    touch=touch(
        edit(BASELINE, (9, 9), digest=("abc123abc123",)),
        edit(BASELINE_TEST, (70, 70), digest=("def456def456",)),
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


def test_same_file_and_four_shared_title_words_is_a_duplicate_without_a_shared_line() -> (
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
    assert {"worker", "controller", "guard", "host"} <= set(
        result.evidence[0].detail.replace(",", "").split()
    )


def test_three_shared_words_on_one_file_are_not_enough() -> None:
    other = touch(edit(GUARD, (300, 301)))
    result = run(
        repo="example_repo",
        head_ref="b3",
        base="main",
        title="fix: a codex worker on a lab host reads a file",
        touch=other,
        open_prs=(OIC_1320,),
    )

    assert result.state == "clear"


def test_the_pull_request_to_adopt_comes_from_the_strongest_evidence_not_the_lowest_number() -> (
    None
):
    wordy = ModelOpenPr(
        number=1106,
        head_ref="old",
        base="main",
        title="fix: Codex worker host controller guard",
        touch=touch(edit(GUARD, (300, 301))),
    )
    result = run(
        repo="example_repo",
        head_ref="new",
        base="main",
        title="fix(OMN-19998): a Codex worker on a lab host keeps the controller guard",
        touch=OIC_1326_TOUCH,
        open_prs=(wordy, OIC_1320),
    )

    assert result.adopt_pr == "example_repo#1320"
    assert [e.kind for e in result.evidence] == [
        EnumDuplicateEvidence.LINES,
        EnumDuplicateEvidence.FILES_AND_TITLE,
    ]


def test_many_same_file_overlaps_are_counted_not_listed() -> None:
    crowd = tuple(
        ModelOpenPr(
            number=100 + n,
            head_ref=f"h{n}",
            base="dev",
            title=f"unrelated change {n}",
            touch=touch(edit(BASELINE, (50 + n, 50 + n))),
        )
        for n in range(9)
    )
    result = run(
        repo="omnibase_core",
        head_ref="mine",
        base="dev",
        title="something else entirely",
        touch=touch(edit(BASELINE, (1, 1))),
        open_prs=crowd,
    )

    assert result.state == "clear"
    assert len(result.notes) == 6
    assert result.notes[-1].startswith("4 more")


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
    lines, shared = overlap(generic, other.touch, GENERIC, "omnibase_core")

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


# ---- generated files: every plan pull request rewrites the count rows of a rendered index (2026-10-30)
#
# The caller reads the repository's generated paths from the deployment overlay and passes them in the request.
# The fixtures use a made-up repository so the public tests carry no private name.

PLANS = "example_plans_repo"
INDEX = "plans/INDEX.md"
REGISTRY = "plans/PLAN_LIFECYCLE.yaml"
DECLARED = {PLANS: (INDEX,)}
#: ``check_plan_index.py --write`` rewrites the status-count rows at lines 16 and 19 in every plan pull request.
COUNT_ROWS = (16, 16), (19, 19)


def plan_pr(number: int, plan: str, *plan_spans: tuple[int, int]) -> ModelOpenPr:
    """A plan pull request: its plan file, its registry entry, and the regenerated count rows of the index."""
    return ModelOpenPr(
        number=number,
        head_ref=f"jonah/plan-{number}",
        base="main",
        title=f"docs: the {plan} plan",
        touch=touch(
            ModelFileTouch(
                path=f"plans/{plan}.md",
                added=not plan_spans,
                spans=plan_spans,
                digest=("abc123abc123",),
            ),
            edit(INDEX, *COUNT_ROWS, digest=("def456def456",)),
            ModelFileTouch(path=REGISTRY),
        ),
    )


def fold(
    mine: ModelOpenPr,
    *open_prs: ModelOpenPr,
    repo: str = PLANS,
    generated: dict[str, tuple[str, ...]] | None = None,
) -> ModelPrDuplicateGuardResult:
    return run(
        repo=repo,
        head_ref=mine.head_ref,
        base="main",
        title=mine.title,
        touch=mine.touch,
        open_prs=open_prs,
        generated_paths=DECLARED if generated is None else generated,
    )


OPEN_PLANS = (
    plan_pr(854, "alpha-ledger"),
    plan_pr(1538, "beta-routing"),
    plan_pr(1541, "gamma-dashboards"),
)


def test_plan_pull_requests_that_share_only_the_regenerated_index_rows_pass() -> None:
    result = fold(plan_pr(1, "configuration-contract"), *OPEN_PLANS)

    assert result.state == "clear"
    assert result.evidence == ()


def test_the_declaration_is_what_clears_the_index_rows() -> None:
    mine = plan_pr(1, "configuration-contract")

    undeclared = fold(mine, *OPEN_PLANS, generated={})

    assert undeclared.state == "adopt"
    assert {e.kind for e in undeclared.evidence} == {EnumDuplicateEvidence.LINES}
    assert all(INDEX in e.detail for e in undeclared.evidence)
    assert fold(mine, *OPEN_PLANS).state == "clear"


def test_the_repository_is_matched_by_its_short_name_in_either_spelling() -> None:
    mine = plan_pr(1, "configuration-contract")

    assert fold(mine, *OPEN_PLANS, repo="SomeOrg/example-plans-repo").state == "clear"
    assert (
        fold(
            mine,
            *OPEN_PLANS,
            generated={"SomeOrg/Example-Plans-Repo": (INDEX,)},
        ).state
        == "clear"
    )


def test_two_pull_requests_editing_the_same_plan_file_are_still_refused() -> None:
    result = fold(
        plan_pr(2, "alpha-ledger", (44, 60)),
        plan_pr(854, "alpha-ledger", (40, 52)),
        plan_pr(1538, "beta-routing"),
    )

    assert result.state == "adopt"
    assert result.adopt_pr == f"{PLANS}#854"
    assert [e.kind for e in result.evidence] == [EnumDuplicateEvidence.LINES]
    assert "plans/alpha-ledger.md:44" in result.evidence[0].detail
    assert INDEX not in result.evidence[0].detail


def test_two_pull_requests_adding_the_same_plan_file_are_still_refused() -> None:
    result = fold(plan_pr(2, "alpha-ledger"), plan_pr(854, "alpha-ledger"))

    assert result.state == "adopt"
    assert "plans/alpha-ledger.md (both add it)" in result.evidence[0].detail


def test_a_repository_that_does_not_declare_the_index_generated_still_has_it_counted() -> (
    None
):
    result = fold(
        plan_pr(1, "configuration-contract"), *OPEN_PLANS, repo="example_repo"
    )

    assert result.state == "adopt"
    assert all(INDEX in e.detail for e in result.evidence)


def test_a_generated_path_names_one_file_not_every_file_of_that_name() -> None:
    rules = ModelDuplicateGuardRules(generated_paths=DECLARED)
    other = touch(edit("docs/INDEX.md", (16, 16)))
    lines, shared = overlap(other, other, rules, PLANS)

    assert shared == ["docs/INDEX.md"]
    assert lines == ["docs/INDEX.md:16"]


def test_the_shipped_generic_files_come_from_the_contract() -> None:
    assert "*.lock" in GENERIC.generic_paths
    assert GENERIC.ignores(PLANS, "uv.lock")
    assert GENERIC.ignores(PLANS, "deep/dir/package-lock.json")
    assert not GENERIC.ignores(PLANS, INDEX)


def test_the_handler_source_holds_no_file_list() -> None:
    source = Path(handler_module.__file__).read_text(encoding="utf-8")

    assert "INDEX.md" not in source
    assert "package-lock" not in source
    assert "os.environ" not in source


# ---- intent, stack ancestry and draft ownership (2026-10-31)


@pytest.mark.parametrize(
    ("ticket", "mine", "theirs", "expected"),
    [
        ("OMN-2", ("aaaaaaaaaaaa",), ("bbbbbbbbbbbb",), "clear"),
        ("OMN-1", ("aaaaaaaaaaaa",), ("bbbbbbbbbbbb",), "clear"),
        ("OMN-1", ("a", "b", "c"), ("a", "d", "e", "f"), "adopt"),
        ("OMN-1", (), (), "adopt"),
        ("OMN-2", ("aaaaaaaaaaaa",), ("aaaaaaaaaaaa",), "adopt"),
        ("OMN-2", (), (), "clear"),
        ("OMN-2", (), ("aaaaaaaaaaaa",), "clear"),
        ("OMN-2", ("a", "b", "c"), ("a", "b", "c", "d", "e"), "adopt"),
        ("OMN-2", ("a", "b", "c"), ("a", "b", "d", "e"), "clear"),
    ],
)
def test_intent_line_overlap_needs_ticket_or_same_change(
    ticket: str, mine: tuple[str, ...], theirs: tuple[str, ...], expected: str
) -> None:
    result = run(
        repo="example_repo",
        head_ref=f"fix/{ticket}-new",
        base="dev",
        title="new behavior",
        touch=touch(edit(GUARD, (204, 204), digest=mine)),
        open_prs=(
            ModelOpenPr(
                number=1,
                head_ref="fix/omn-1-old",
                base="dev",
                title="old behavior",
                touch=touch(edit(GUARD, (204, 204), digest=theirs)),
            ),
        ),
    )
    assert result.state == expected
    if expected == "clear":
        assert result.evidence == ()
        why = "same ticket" if ticket == "OMN-1" else "other ticket"
        assert result.notes == (
            f"example_repo#1 also changes {GUARD}:204 but carries a different intent ({why}, different change): not the same fix",
        )
    else:
        assert result.evidence[0].kind is EnumDuplicateEvidence.LINES


def test_intent_same_ticket_without_overlap_and_unrelated_titles_is_clear() -> None:
    result = run(
        repo="example_repo",
        head_ref="omn-19998-other",
        base="main",
        title="metrics export",
        touch=touch(edit(GUARD, (400, 400))),
        open_prs=(OIC_1320,),
    )
    assert result.state == "clear"


@pytest.mark.parametrize("source", ["branch", "title", "own_claim"])
def test_intent_all_tickets_in_branch_title_and_own_live_claim(source: str) -> None:
    existing = CORE_2159.model_copy(
        update={
            "head_ref": "omn-50-and-omn-51",
            "title": "also OMN-52",
            "touch": RENAME,
        }
    )
    result = run(
        repo="omnibase_core",
        head_ref="omn-52-new" if source == "branch" else "mine",
        title="also omn-51" if source == "title" else "unrelated",
        base="dev",
        lane="me",
        ledger_text=("2026-10-30T00:30:00Z | CLAIM | lane=me | ticket=omn-50\n")
        if source == "own_claim"
        else "",
        touch=touch(edit(BASELINE, (9, 9))),
        open_prs=(existing,),
    )
    assert result.evidence[0].kind is EnumDuplicateEvidence.LINES


def test_intent_ticket_word_boundaries_exclude_partial_matches() -> None:
    result = run(
        repo="example_repo",
        head_ref="prefixOMN-19998suffix",
        base="main",
        title="unrelated",
        touch=touch(edit(GUARD, (204, 204))),
        open_prs=(OIC_1320,),
    )
    assert result.state == "clear"


def test_intent_similarity_uses_only_line_hit_files() -> None:
    mine = touch(
        edit(GUARD, (204, 204), digest=("a",)),
        edit(WORKER, (10, 10), digest=("b", "c", "d")),
    )
    other = OIC_1320.model_copy(
        update={
            "touch": touch(
                edit(GUARD, (204, 204), digest=("z",)),
                edit(WORKER, (20, 20), digest=("b", "c", "d")),
            )
        }
    )
    result = run(
        repo="example_repo", head_ref="mine", base="main", touch=mine, open_prs=(other,)
    )
    assert result.state == "clear"


@pytest.mark.parametrize("root_base", ["dev", "A"])
def test_stacked_base_name_chain_skips_all_ancestors(root_base: str) -> None:
    prs = tuple(
        CORE_2159.model_copy(update={"number": n, "head_ref": head, "base": base})
        for n, head, base in ((1, "A", "B"), (2, "B", "C"), (3, "C", root_base))
    )
    result = run(
        repo="omnibase_core", head_ref="new", base="A", touch=RENAME, open_prs=prs
    )
    assert result.state == "clear"
    assert result.notes == result.unread == result.evidence == ()


def test_stacked_flag_skips_even_shared_intent_and_cause() -> None:
    result = run(
        repo="omnibase_core",
        head_ref="new",
        base="dev",
        touch=RENAME,
        body="Fix-Check: CI Summary",
        open_prs=(
            CORE_2159.model_copy(
                update={
                    "stacked": True,
                    "body": "Fix-Check: CI Summary",
                }
            ),
        ),
    )
    assert result.state == "clear"
    assert result.notes == result.unread == result.evidence == ()


@pytest.mark.parametrize(
    "owner",
    [
        "none",
        "pr",
        "fix",
        "adopted",
        "ticket",
        "expired",
        "released",
        "other_repo",
        "non_draft",
    ],
)
def test_ownerless_draft_refuses_only_with_live_owner(owner: str) -> None:
    existing = CORE_2159.model_copy(update={"draft": owner != "non_draft"})
    cell = (
        f"{owner}=SomeOrg/omnibase_core#2159"
        if owner in {"pr", "fix", "adopted"}
        else "ticket=OMN-21533"
    )
    ledger = f"2026-10-30T00:30:00Z | CLAIM | lane=owner | {cell}\n"
    if owner in {"none", "non_draft"}:
        ledger = ""
    elif owner == "expired":
        ledger = ledger.replace("00:30:00", "00:00:00").replace(
            "2026-10-30", "2026-10-29"
        )
    elif owner == "released":
        ledger += "2026-10-30T00:35:00Z | TERMINAL | lane=owner | outcome=done\n"
    elif owner == "other_repo":
        ledger = ledger.replace("ticket=OMN-21533", "pr=other_repo#2159")
    result = run(
        repo="omnibase_core",
        head_ref="omn-21533-new",
        base="dev",
        touch=RENAME,
        ledger_text=ledger,
        open_prs=(existing,),
    )
    if owner in {"none", "expired", "released", "other_repo"}:
        assert result.state == "clear"
        assert "is a draft with no live owner CLAIM" in result.notes[0]
        assert "evidence only, adopt or supersede it, not a refusal" in result.notes[0]
    else:
        assert result.state == "adopt"
        assert result.evidence[0].kind is EnumDuplicateEvidence.LINES


def test_ownerless_draft_same_branch_always_refuses() -> None:
    result = run(
        repo="omnibase_core",
        head_ref=CORE_2159.head_ref,
        open_prs=(CORE_2159.model_copy(update={"draft": True}),),
    )
    assert result.evidence[0].kind is EnumDuplicateEvidence.BRANCH


@pytest.mark.parametrize(
    "kind", [EnumDuplicateEvidence.FILES_AND_TITLE, EnumDuplicateEvidence.CAUSE_CITED]
)
def test_ownerless_draft_title_and_cause_are_evidence_only(
    kind: EnumDuplicateEvidence,
) -> None:
    existing = OIC_1320.model_copy(
        update={"draft": True, "body": "Fix-Check: CI Summary"}
    )
    result = run(
        repo="example_repo",
        head_ref="mine",
        base="main",
        title=existing.title
        if kind is EnumDuplicateEvidence.FILES_AND_TITLE
        else "unrelated",
        body="Fix-Check: CI Summary"
        if kind is EnumDuplicateEvidence.CAUSE_CITED
        else "",
        touch=touch(edit(GUARD, (400, 400))),
        open_prs=(existing,),
    )
    assert result.state == "clear"
    assert any("evidence only" in note for note in result.notes)


GRANTS = "src/omnibase_infra/runtime/db/table_grant_derivation.py"


def test_recorded_contract_derived_grants_removal_is_not_tuple_neighbors() -> None:
    prs = tuple(
        ModelOpenPr(
            number=n,
            head_ref=f"omn-{ticket}-tuple-work",
            base="dev",
            title=f"OMN-{ticket}: {title}",
            draft=draft,
            touch=touch(edit(GRANTS, (527, 546), digest=(digest,))),
        )
        for n, ticket, title, draft, digest in (
            (5311, 17292, "add a grant", True, "111111111111"),
            (5318, 17300, "add another grant", True, "222222222222"),
            *(
                (n, 18000 + n, "lease projection grant", True, "333333333333")
                for n in (5368, 5371, 5372, 5373, 5375, 5376, 5406)
            ),
            (5469, 17427, "compact tuple entries", False, "444444444444"),
        )
    )
    result = run(
        repo="omnibase_infra",
        head_ref="omn-20134-contract-derived-grants",
        base="dev",
        title="derive grants from contracts",
        touch=touch(edit(GRANTS, (527, 545), digest=("aaaaaaaaaaaa", "bbbbbbbbbbbb"))),
        open_prs=prs,
    )
    assert result.state == "clear"
    assert result.evidence == ()
    assert all("different intent" in note for note in result.notes[:-1])


def test_recorded_gateway_off_is_not_stack_parent_or_unrelated_drafts() -> None:
    path = "src/omnibase_infra/runtime/dev201_gateway.py"
    prs = tuple(
        ModelOpenPr(
            number=n,
            head_ref=head,
            base="dev",
            title=f"OMN-{ticket}: {title}",
            draft=n != 5481,
            stacked=n == 5481,
            touch=touch(edit(path, (80, 90), digest=("bbbbbbbbbbbb",))),
        )
        for n, ticket, head, title in (
            (
                5481,
                21588,
                "jonah/omn-21588-health-monitor-single-filter",
                "single health filter",
            ),
            (5351, 18001, "omn-18001-lease", "lease configuration"),
            (5400, 18002, "omn-18002-monitor", "monitor timing"),
            (5401, 18003, "omn-18003-routing", "routing configuration"),
        )
    )
    result = run(
        repo="omnibase_infra",
        head_ref="jonah/omn-21629-dev201-gateway-off",
        base="dev",
        title="OMN-21629: disable dev201 gateway",
        touch=touch(edit(path, (80, 90), digest=("aaaaaaaaaaaa",))),
        open_prs=prs,
    )
    assert result.state == "clear"
    assert result.evidence == ()
    assert len(result.notes) == 3
    assert not any("5481" in note for note in result.notes)


def test_recorded_real_duplicate_still_refuses_with_lines_and_pr_named() -> None:
    changed = touch(edit(GRANTS, (527, 545), digest=("aaaaaaaaaaaa",)))
    result = run(
        repo="omnibase_infra",
        head_ref="omn-20134-contract-derived-grants",
        base="dev",
        touch=changed,
        open_prs=(
            ModelOpenPr(
                number=5500,
                head_ref="jonah/omn-20134-grants",
                base="dev",
                title="derive grants",
                touch=changed,
            ),
        ),
    )
    assert result.evidence[0].kind is EnumDuplicateEvidence.LINES
    assert result.adopt_pr == "omnibase_infra#5500"
    assert "omnibase_infra#5500" in result.message


def test_intent_contract_owns_ticket_pattern_and_similarity_threshold() -> None:
    contract = yaml.safe_load((handler_module.NODE_ROOT / "contract.yaml").read_text())
    assert contract["config"]["intent"] == {
        "ticket_pattern": "OMN-[0-9]+",
        "min_same_change_similarity": 0.6,
        "min_same_ticket_similarity": 0.1,
    }
    source = Path(handler_module.__file__).read_text()
    assert "OMN-" not in source
    assert "0.6" not in source
