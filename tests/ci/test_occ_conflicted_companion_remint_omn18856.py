# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Behaviour of the conflicted-companion re-mint trigger (OMN-18856).

The cases are real. ``fixtures/omn18856_remint/live_reads_2026-09-26.json``
holds the ``gh`` responses the live client read at 2026-09-26T23:00Z, trimmed
to the fields it parses, for twelve open onex_change_control PRs:

* six companions a later ``dev`` commit to their own ``contracts/<ticket>.yaml``
  put into conflict (OCC#11541, #11543, #11488, #11120, #11103, #11537) --
  confirmed independently with ``git merge-tree`` against ``origin/dev``, every
  conflict confined to that one contract file;
* two conflicted companions whose product PR is already merged or closed
  (OCC#11485 for ``omniweb#441``, OCC#11526 for ``omnibase_infra#4187``);
* two clean companions (OCC#11498, OCC#11245);
* the observation-append PR OCC#11441, which conflicts but has no producer to
  replay; and the per-ticket batch companion OCC#11437.

The replay runs through :class:`GhCli` itself, so the parsing the scheduled job
depends on is what is under test, not a restatement of it.

Falsifiers:

* AC1 -- every sibling-conflicted companion with an open product PR yields
  exactly one autobind publish for that product PR, and nothing else does.
* AC2 -- the trigger cannot loop: a companion re-minted after the sibling that
  conflicted it is reported ``stuck_after_remint`` and never re-published.
* AC3 -- a re-mint's dropped executed receipts get one receipt-runner dispatch
  per companion head for this repository's PRs, and none for other repos.
* AC4 -- dry-run writes nothing; any unreadable companion fails the pass.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from scripts.ci.occ_conflicted_companion_remint import (  # noqa: E402
    MACHINE_MINTED_LABEL,
    OCC_REPO_DEFAULT,
    RECEIPT_RUNNER_RUN_NAME,
    UNREAD_HEAD_SHA,
    CompanionFacts,
    EnumReceiptOutcome,
    EnumRemintOutcome,
    GhCli,
    ProductFacts,
    Target,
    companion_branch_for,
    decide_receipt_refire,
    decide_remint,
    pending_receipt_dirs,
    resolve_target,
    run_pass,
)

pytestmark = pytest.mark.unit

FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "omn18856_remint"
    / "live_reads_2026-09-26.json"
)
THIS_REPO = "OmniNode-ai/omnimarket"

SIBLING_CONFLICTED = {
    11541: ("OmniNode-ai/omnimarket", 2973),
    11543: ("OmniNode-ai/omnibase_infra", 4193),
    11488: ("OmniNode-ai/omnimarket", 2955),
    11120: ("OmniNode-ai/omnimarket", 2857),
    11103: ("OmniNode-ai/omnimarket", 2853),
    11537: ("OmniNode-ai/omnibase_infra", 4190),
}
PRODUCT_GONE = {11485: "merged", 11526: "closed"}
CLEAN = (11498, 11245)


class ReplayGh(GhCli):
    """The real client, answering every ``gh`` call from the captured responses."""

    def __init__(self, calls: dict[str, dict[str, object]]) -> None:
        super().__init__(REPO_ROOT / "scripts" / "publish_occ_autobind_command.py")
        self._calls = calls
        self._sleep = lambda _seconds: None
        self.published: list[tuple[Target, ProductFacts]] = []
        self.dispatched: list[tuple[str, int]] = []
        self.runner_ran: set[int] = set()
        self.numbers: tuple[int, ...] = ()

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        key = " ".join(args)
        if key not in self._calls:
            raise AssertionError(f"unrecorded gh call: {key}")
        rec = self._calls[key]
        return subprocess.CompletedProcess(
            ["gh", *args],
            int(str(rec["returncode"])),
            str(rec["stdout"]),
            str(rec["stderr"]),
        )

    def open_companion_numbers(self, *, occ_repo: str) -> tuple[int, ...]:
        return self.numbers

    def receipt_runner_ran_since(
        self, *, repo: str, pr_number: int, since: datetime
    ) -> bool:
        return pr_number in self.runner_ran

    def publish_autobind(
        self, *, target: Target, product: ProductFacts, lane: str
    ) -> str:
        self.published.append((target, product))
        return "Published onex.cmd.omnimarket.occ-autobind.v1 event_id=test"

    def dispatch_receipt_runner(self, *, repo: str, pr_number: int) -> None:
        self.dispatched.append((repo, pr_number))


@pytest.fixture(scope="module")
def recorded() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def gh(recorded: dict[str, object]) -> ReplayGh:
    calls = recorded["calls"]
    assert isinstance(calls, dict)
    return ReplayGh(calls)


@pytest.fixture(scope="module")
def captured_at(recorded: dict[str, object]) -> datetime:
    return datetime.fromisoformat(str(recorded["captured_at"]))


def _verdict(gh: ReplayGh, number: int) -> tuple[CompanionFacts, ProductFacts, object]:
    companion = gh.companion(occ_repo=OCC_REPO_DEFAULT, number=number)
    target = resolve_target(companion)
    product = (
        gh.product(repo=target.repo, pr_number=target.pr_number)
        if isinstance(target, Target)
        else ProductFacts(readable=False)
    )
    newest = None
    if companion.mergeable is False and companion.mergeable_state == "dirty":
        for path in companion.contract_paths:
            when = gh.newest_commit_since(
                occ_repo=OCC_REPO_DEFAULT, path=path, since=companion.head_committed_at
            )
            if when is not None and (newest is None or when > newest):
                newest = when
    return companion, product, decide_remint(companion, newest, product)


# --- AC1: real sibling conflicts are re-minted, nothing else is ------------


@pytest.mark.parametrize("number", sorted(SIBLING_CONFLICTED))
def test_live_sibling_conflict_is_reminted_for_its_own_product_pr(
    gh: ReplayGh, number: int
) -> None:
    companion, _product, decision = _verdict(gh, number)
    assert decision.outcome is EnumRemintOutcome.REMINT, decision.reason
    assert decision.target is not None
    repo, pr = SIBLING_CONFLICTED[number]
    assert (decision.target.repo, decision.target.pr_number) == (repo, pr)
    assert companion.head_ref == companion_branch_for(repo, pr)
    assert companion.contract_paths, "the conflict class is the shared contract"
    assert decision.target.ticket.startswith("OMN-")


@pytest.mark.parametrize("number", sorted(PRODUCT_GONE))
def test_conflicted_companion_of_a_finished_product_pr_is_not_replayed(
    gh: ReplayGh, number: int
) -> None:
    companion, product, decision = _verdict(gh, number)
    assert companion.mergeable is False
    assert decision.outcome is EnumRemintOutcome.PRODUCT_NOT_OPEN
    assert PRODUCT_GONE[number] in decision.reason
    assert product.readable


@pytest.mark.parametrize("number", CLEAN)
def test_clean_companion_is_left_alone(gh: ReplayGh, number: int) -> None:
    _companion, _product, decision = _verdict(gh, number)
    assert decision.outcome is EnumRemintOutcome.MERGEABLE


def test_observation_append_pr_is_not_a_producer_companion(gh: ReplayGh) -> None:
    companion, _product, decision = _verdict(gh, 11441)
    assert MACHINE_MINTED_LABEL not in companion.labels
    assert decision.outcome is EnumRemintOutcome.NOT_MACHINE_MINTED


def test_batch_companion_has_no_single_product_pr(gh: ReplayGh) -> None:
    _companion, _product, decision = _verdict(gh, 11437)
    assert decision.outcome is EnumRemintOutcome.BATCH_BRANCH


def test_run_pass_publishes_exactly_the_sibling_conflicted_set(
    gh: ReplayGh, captured_at: datetime
) -> None:
    gh.numbers = (
        *sorted(SIBLING_CONFLICTED),
        *sorted(PRODUCT_GONE),
        *CLEAN,
        11437,
    )
    report = run_pass(
        gh,
        occ_repo=OCC_REPO_DEFAULT,
        this_repo=THIS_REPO,
        lane="dev",
        dry_run=False,
        only=None,
        now=captured_at,
    )
    assert report.errors == ()
    published = sorted((t.repo, t.pr_number) for t, _ in gh.published)
    assert published == sorted(SIBLING_CONFLICTED.values())
    reminted = [n for n, d in report.remint if d.outcome is EnumRemintOutcome.REMINT]
    assert sorted(reminted) == sorted(SIBLING_CONFLICTED)
    # Every published product PR is public here, so its real head is carried.
    assert all(p.head_sha and p.head_sha != UNREAD_HEAD_SHA for _, p in gh.published)


# --- AC2: bounded, and fail-closed on what it cannot read --------------------

# omniclaude#2367 / OCC#11425, the live proof: conflicted by a dev commit to
# contracts/OMN-13902.yaml at 07:35:41Z, head d9face0dd9 from 06:42:44Z. One
# autobind replay at 22:46Z re-minted it to c4b0553785 (22:47:06Z), clean by
# git merge-tree, and it merged at 22:56:10Z.
_OCC_11425_BEFORE = CompanionFacts(
    number=11425,
    title=(
        "evidence(OMN-13902): OCC Evidence-Source autobind for "
        "OmniNode-ai/omniclaude#2367"
    ),
    head_ref="auto/omninode-ai-omniclaude-pr-2367-occ-autobind",
    head_sha="d9face0dd92eb940b058cd7d91c7289daf50a3b4",
    draft=False,
    labels=(MACHINE_MINTED_LABEL, "ci:ready"),
    mergeable=False,
    mergeable_state="dirty",
    head_committed_at=datetime(2026, 9, 26, 6, 42, 44, tzinfo=UTC),
    files=(
        "contracts/OMN-13902.yaml",
        "drift/dod_receipts/OMN-13902/occ-self-bind-pr-11425/command.yaml",
    ),
)
_SIBLING_11426 = datetime(2026, 9, 26, 7, 35, 41, tzinfo=UTC)
_PRODUCT_2367 = ProductFacts(
    readable=True,
    state="open",
    body="Summary\n\nEvidence-Source: OCC#11425\n",
    head_sha="ee5b6aa2facad9894306577bd83b9db7c37315d1",
    title="feat(OMN-13902): example",
)


def test_live_proof_case_before_the_remint_is_reminted() -> None:
    decision = decide_remint(_OCC_11425_BEFORE, _SIBLING_11426, _PRODUCT_2367)
    assert decision.outcome is EnumRemintOutcome.REMINT
    assert decision.target == Target("OmniNode-ai/omniclaude", 2367, "OMN-13902")


def test_live_proof_case_after_the_remint_is_left_alone() -> None:
    after = replace(
        _OCC_11425_BEFORE,
        head_sha="c4b0553785",
        mergeable=True,
        mergeable_state="blocked",
        head_committed_at=datetime(2026, 9, 26, 22, 47, 6, tzinfo=UTC),
    )
    assert (
        decide_remint(after, None, _PRODUCT_2367).outcome is EnumRemintOutcome.MERGEABLE
    )


def test_a_remint_that_did_not_clear_the_conflict_is_never_retried() -> None:
    still_dirty = replace(
        _OCC_11425_BEFORE,
        head_committed_at=datetime(2026, 9, 26, 22, 47, 6, tzinfo=UTC),
    )
    decision = decide_remint(still_dirty, _SIBLING_11426, _PRODUCT_2367)
    assert decision.outcome is EnumRemintOutcome.STUCK_AFTER_REMINT


def test_a_new_sibling_after_a_remint_triggers_one_more() -> None:
    reminted = replace(
        _OCC_11425_BEFORE,
        head_committed_at=datetime(2026, 9, 26, 22, 47, 6, tzinfo=UTC),
    )
    later_sibling = datetime(2026, 9, 26, 23, 5, 0, tzinfo=UTC)
    assert (
        decide_remint(reminted, later_sibling, _PRODUCT_2367).outcome
        is EnumRemintOutcome.REMINT
    )


def test_unknown_mergeability_waits_for_the_next_pass() -> None:
    unknown = replace(_OCC_11425_BEFORE, mergeable=None, mergeable_state="unknown")
    assert (
        decide_remint(unknown, _SIBLING_11426, _PRODUCT_2367).outcome
        is EnumRemintOutcome.MERGEABILITY_UNKNOWN
    )


def test_unmergeable_for_a_reason_other_than_a_conflict_is_not_reminted() -> None:
    blocked = replace(_OCC_11425_BEFORE, mergeable_state="blocked")
    assert (
        decide_remint(blocked, _SIBLING_11426, _PRODUCT_2367).outcome
        is EnumRemintOutcome.NOT_DIRTY
    )


def test_conflict_without_a_sibling_contract_commit_is_not_this_class() -> None:
    assert (
        decide_remint(_OCC_11425_BEFORE, None, _PRODUCT_2367).outcome
        is EnumRemintOutcome.NO_SIBLING_COMMIT
    )


@pytest.mark.parametrize(
    "body",
    [
        "no evidence line at all",
        "Evidence-Source: OCC#99999\n",
        "Evidence-Source: OCC#11425\nEvidence-Source: OCC#11426\n",
    ],
)
def test_a_body_not_bound_to_exactly_this_companion_is_not_replayed(body: str) -> None:
    decision = decide_remint(
        _OCC_11425_BEFORE, _SIBLING_11426, replace(_PRODUCT_2367, body=body)
    )
    assert decision.outcome is EnumRemintOutcome.PRODUCT_UNBOUND


def test_draft_product_pr_is_not_replayed() -> None:
    decision = decide_remint(
        _OCC_11425_BEFORE, _SIBLING_11426, replace(_PRODUCT_2367, draft=True)
    )
    assert decision.outcome is EnumRemintOutcome.PRODUCT_DRAFT


def test_title_and_branch_naming_different_prs_is_refused() -> None:
    forged = replace(
        _OCC_11425_BEFORE,
        title="evidence(OMN-13902): OCC companion for OmniNode-ai/omniclaude#2368",
    )
    decision = decide_remint(forged, _SIBLING_11426, _PRODUCT_2367)
    assert decision.outcome is EnumRemintOutcome.IDENTITY_MISMATCH


def test_private_product_pr_is_left_to_the_emitters_own_checks() -> None:
    decision = decide_remint(
        _OCC_11425_BEFORE, _SIBLING_11426, ProductFacts(readable=False)
    )
    assert decision.outcome is EnumRemintOutcome.REMINT


def test_an_unreadable_companion_fails_the_pass_instead_of_reading_clean(
    gh: ReplayGh, captured_at: datetime
) -> None:
    gh.numbers = (424242,)
    report = run_pass(
        gh,
        occ_repo=OCC_REPO_DEFAULT,
        this_repo=THIS_REPO,
        lane="dev",
        dry_run=False,
        only=None,
        now=captured_at,
    )
    assert report.errors
    assert "OCC#424242" in report.errors[0]
    assert gh.published == []


def test_dry_run_publishes_and_dispatches_nothing(
    gh: ReplayGh, captured_at: datetime
) -> None:
    gh.numbers = (*sorted(SIBLING_CONFLICTED), 11245)
    report = run_pass(
        gh,
        occ_repo=OCC_REPO_DEFAULT,
        this_repo=THIS_REPO,
        lane="dev",
        dry_run=True,
        only=None,
        now=captured_at + timedelta(hours=1),
    )
    assert report.errors == ()
    assert gh.published == []
    assert gh.dispatched == []
    assert any(d.outcome is EnumRemintOutcome.REMINT for _, d in report.remint)


# --- AC3: executed receipts are re-requested once per re-minted head ---------


def test_pending_receipt_detection_on_live_companions(gh: ReplayGh) -> None:
    executed = gh.companion(occ_repo=OCC_REPO_DEFAULT, number=11488)
    pending = gh.companion(occ_repo=OCC_REPO_DEFAULT, number=11245)
    assert pending_receipt_dirs(executed.files) == ()
    assert pending_receipt_dirs(pending.files) == (
        "drift/dod_receipts/OMN-18143/dod-occ-diff-derived-behavior-proof-pr-2900",
    )


def test_receipt_runner_is_dispatched_once_for_a_pending_head(
    gh: ReplayGh, captured_at: datetime
) -> None:
    gh.numbers = (11245,)
    run_pass(
        gh,
        occ_repo=OCC_REPO_DEFAULT,
        this_repo=THIS_REPO,
        lane="dev",
        dry_run=False,
        only=None,
        now=captured_at,
    )
    assert gh.dispatched == [(THIS_REPO, 2900)]

    gh.dispatched.clear()
    gh.runner_ran.add(2900)
    run_pass(
        gh,
        occ_repo=OCC_REPO_DEFAULT,
        this_repo=THIS_REPO,
        lane="dev",
        dry_run=False,
        only=None,
        now=captured_at,
    )
    assert gh.dispatched == []


def test_receipt_runner_waits_for_the_event_driven_run_on_a_fresh_head(
    gh: ReplayGh,
) -> None:
    companion = gh.companion(occ_repo=OCC_REPO_DEFAULT, number=11245)
    target = resolve_target(companion)
    assert isinstance(target, Target)
    product = gh.product(repo=target.repo, pr_number=target.pr_number)
    decision = decide_receipt_refire(
        companion,
        target,
        product,
        this_repo=THIS_REPO,
        runner_ran_since_head=False,
        now=companion.head_committed_at + timedelta(minutes=1),
    )
    assert decision.outcome is EnumReceiptOutcome.TOO_FRESH


def test_receipt_runner_is_never_dispatched_for_another_repo(
    gh: ReplayGh, captured_at: datetime
) -> None:
    companion = gh.companion(occ_repo=OCC_REPO_DEFAULT, number=11245)
    decision = decide_receipt_refire(
        companion,
        Target("OmniNode-ai/omnibase_infra", 2900, "OMN-18143"),
        ProductFacts(readable=True, state="open"),
        this_repo=THIS_REPO,
        runner_ran_since_head=False,
        now=captured_at,
    )
    assert decision.outcome is EnumReceiptOutcome.OTHER_REPO


def test_receipt_runner_waits_while_the_companion_still_conflicts(
    gh: ReplayGh, captured_at: datetime
) -> None:
    gh.numbers = (11541,)
    report = run_pass(
        gh,
        occ_repo=OCC_REPO_DEFAULT,
        this_repo=THIS_REPO,
        lane="dev",
        dry_run=False,
        only=None,
        now=captured_at,
    )
    assert [r.outcome for _, r in report.receipts] == [EnumReceiptOutcome.NOT_MERGEABLE]
    assert gh.dispatched == []


# --- wiring ---------------------------------------------------------------


def _workflow(name: str) -> dict[object, object]:
    loaded = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
    )
    assert isinstance(loaded, dict)
    return loaded


def test_receipt_runner_run_name_is_the_key_the_heal_reads() -> None:
    runner = _workflow("occ-receipt-runner.yml")
    expected = RECEIPT_RUNNER_RUN_NAME.format(
        pr="${{ github.event.pull_request.number || inputs.pr_number }}"
    )
    assert runner["run-name"] == expected
    # yaml reads the bare key ``on`` as True.
    triggers = runner[True]
    assert isinstance(triggers, dict)
    assert "workflow_dispatch" in triggers
    assert "pull_request" in triggers


def test_remint_workflow_is_scheduled_and_is_not_a_gate() -> None:
    wf = _workflow("occ-conflicted-companion-remint.yml")
    triggers = wf[True]
    assert isinstance(triggers, dict)
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    text = (
        REPO_ROOT / ".github" / "workflows" / "occ-conflicted-companion-remint.yml"
    ).read_text(encoding="utf-8")
    assert "continue-on-error" not in text
    assert "scripts/ci/occ_conflicted_companion_remint.py" in text
