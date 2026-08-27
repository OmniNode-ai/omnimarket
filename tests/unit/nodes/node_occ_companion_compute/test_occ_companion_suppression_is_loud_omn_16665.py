# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16665: a mint decline must be legible, and a LOST companion must be loud.

Before this ticket every declining branch of ``compute_companion_plan`` returned
the same shape — ``no_op=True`` plus free text — which the write-EFFECT logged on
the ``.201`` runtime and discarded. Two failures follow from that, and this
module holds the RED controls for both.

**Failure 1 — illegible (the filed defect).** ``omnibase_compat#193`` and
``omnidash#292`` each carried an outage-era test-plan line reading "do not merge
until CI is confirmed green post-outage". That matches the F-17 hold rule, so
the mint was suppressed exactly as written and nothing said so anywhere on the
PR. A lane burned two replay attempts, concluded the consumer was dead, and
escalated — while the consumer was minting OCC#7195/#7196/#7199 in the same
window. Naming the rule is not enough (AC2): the surfaced text must quote the
substring that actually matched, because "do not merge **until** X" reads to a
human as conditional and to the regex as absolute.

**Failure 2 — inaudible (the root cause found while fixing failure 1).**
``omnimemory#447`` (OMN-16669) opened 19:35:00Z and merged 19:46:09Z; the
self-hosted runner fleet held the publisher job until 19:47:34Z, so the mint
command reached the broker ~90s AFTER the PR merged. Compute did a correct live
read, matched the closed/merged branch, and no-op'd. The publisher's green was
honest — its contract is "delivered to broker" — but the no-op was emitted to
``occ-companion-effect-completed.v1``, a SUCCESS topic, so every projection and
every "did the mint work" probe read green over a permanently missing evidence
record.

The discrimination this module pins: GitHub's REST ``state`` is ``closed`` for a
merged PR and an abandoned one alike. A closed-UNMERGED PR is a dead target and
declining forever is correct (occ#4333). A MERGED PR that cites a ticket, is
unbound, and is not fast-path-exempt NEEDED the record and will never get one —
that is a defect, and ``evidence_lost`` is how the plan says so.
"""

from __future__ import annotations

import pytest

from omnimarket.events.occ_companion import EnumCompanionSuppressionCode
from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    compute_companion_plan,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
)

_REPO = "OmniNode-ai/omnimemory"
_PR = 447


def _probe() -> ModelObservedProbe:
    return ModelObservedProbe(
        command=f"gh pr view {_PR} --repo {_REPO} --json number,state",
        stdout=f'{{"number":{_PR},"state":"MERGED"}}',
        exit_code=0,
    )


def _request(**overrides: object) -> ModelOccCompanionRequest:
    """A request that AUTHORS by default, so every suppression below is load-bearing."""
    base: dict[str, object] = {
        "repo": _REPO,
        "pr_number": _PR,
        "pr_head_sha": "a" * 40,
        "pr_title": "docs(OMN-16669): correct the settings docstring default",
        "pr_body": "Fixes the drift.\n\nSee OMN-16669 for the acceptance criteria.",
        "pr_state": "open",
        "pr_head_ref": "jonah/omn-16669-fix",
        # Non-trivial, non-infra changed files so the fast-path cannot fire and
        # mask a suppression decision.
        "changed_files": ("src/omnimemory/settings.py", "README.md"),
        "diff_total_lines": 9,
        "run_timestamp": "2026-08-26T19:47:00Z",
        "product_probe": _probe(),
    }
    base.update(overrides)
    return ModelOccCompanionRequest(**base)  # type: ignore[arg-type]


@pytest.mark.unit
class TestControlTheRequestActuallyAuthors:
    def test_open_pr_authors_a_companion(self) -> None:
        """RED control: the shared fixture mints, so a no_op below is caused by
        the override under test and not by an inert request."""
        plan = compute_companion_plan(_request())

        assert plan.no_op is False
        assert plan.suppression is None
        assert plan.companion_files


@pytest.mark.unit
class TestMergedUnboundIsEvidenceLost:
    """The omnimemory#447 race. state=='closed' + merged + unbound == defect."""

    def test_merged_unbound_pr_reports_evidence_lost(self) -> None:
        plan = compute_companion_plan(_request(pr_state="closed", pr_merged=True))

        assert plan.no_op is True
        assert plan.suppression is not None
        assert (
            plan.suppression.code
            is EnumCompanionSuppressionCode.EVIDENCE_LOST_PR_MERGED
        )
        assert plan.suppression.evidence_lost is True
        # The cited ticket must survive onto the plan: a report that cannot name
        # WHICH ticket lost its evidence is not actionable.
        assert plan.tickets == ("OMN-16669",)
        assert "OMN-16669" in plan.suppression.summary

    def test_remediation_names_the_override_that_recovers_it(self) -> None:
        """A decline with no recovery path is a dead end. The OMN-14993 precheck
        docstring says merged replay needs 'a new, deliberately-scoped override
        of F-17'; that override is ``allow_merged_replay`` and the message must
        name it, not merely announce the loss."""
        plan = compute_companion_plan(_request(pr_state="closed", pr_merged=True))

        assert plan.suppression is not None
        assert "allow_merged_replay" in plan.suppression.remediation

    def test_closed_unmerged_pr_is_a_dead_target_not_a_loss(self) -> None:
        """The discrimination that did not exist pre-16665: same REST ``state``,
        opposite verdict. Abandoning a PR loses no evidence."""
        plan = compute_companion_plan(_request(pr_state="closed", pr_merged=False))

        assert plan.no_op is True
        assert plan.suppression is not None
        assert plan.suppression.code is EnumCompanionSuppressionCode.PR_CLOSED_UNMERGED
        assert plan.suppression.evidence_lost is False

    def test_merged_but_already_bound_is_benign(self) -> None:
        """A merged PR that DID get its companion is not a loss. Ordering proof:
        the benign exits must be evaluated before the merged branch concludes."""
        plan = compute_companion_plan(
            _request(
                pr_state="closed",
                pr_merged=True,
                pr_body="Fixes OMN-16669.\n\nEvidence-Source: OCC#7200\n",
            )
        )

        assert plan.suppression is not None
        assert plan.suppression.code is EnumCompanionSuppressionCode.ALREADY_BOUND
        assert plan.suppression.evidence_lost is False

    def test_merged_with_no_ticket_is_benign(self) -> None:
        """No ticket means no contract to author — nothing was lost."""
        plan = compute_companion_plan(
            _request(
                pr_state="closed",
                pr_merged=True,
                pr_title="chore: tidy up",
                pr_body="No ticket here.",
            )
        )

        assert plan.suppression is not None
        assert plan.suppression.code is EnumCompanionSuppressionCode.NO_TICKET
        assert plan.suppression.evidence_lost is False


@pytest.mark.unit
class TestMergedReplayOverride:
    """``allow_merged_replay`` is the recovery path, scoped to MERGED only."""

    def test_override_authors_the_companion_for_a_merged_pr(self) -> None:
        plan = compute_companion_plan(
            _request(pr_state="closed", pr_merged=True, allow_merged_replay=True)
        )

        assert plan.no_op is False
        assert plan.companion_files
        assert plan.tickets == ("OMN-16669",)

    def test_override_does_not_resurrect_a_closed_unmerged_pr(self) -> None:
        """occ#4333's dead-target decline is NOT what this override relaxes. A
        blanket state override would re-open the exact incident F-17 closed."""
        plan = compute_companion_plan(
            _request(pr_state="closed", pr_merged=False, allow_merged_replay=True)
        )

        assert plan.no_op is True
        assert plan.suppression is not None
        assert plan.suppression.code is EnumCompanionSuppressionCode.PR_CLOSED_UNMERGED

    def test_override_defaults_off_so_the_born_path_is_unchanged(self) -> None:
        """Absent the explicit override the merged PR still declines — the born
        path's behavior is identical to pre-16665 apart from the report."""
        plan = compute_companion_plan(_request(pr_state="closed", pr_merged=True))

        assert plan.no_op is True


@pytest.mark.unit
class TestEveryDecliningBranchIsLegible:
    """AC3: every no_op branch carries a structured, quotable suppression."""

    def test_hold_marker_quotes_the_matched_substring_and_line(self) -> None:
        """AC2, against the live omnibase_compat#193 / omnidash#292 text. The
        author must be able to see WHICH words tripped the hold — the phrase is
        conditional in intent and unconditional to the rule."""
        body = (
            "## Summary\n"
            "Adds the badge row.\n"
            "\n"
            "## Test plan\n"
            "CI on this PR may not run; do not merge until CI is confirmed "
            "green post-outage.\n"
        )
        plan = compute_companion_plan(_request(pr_body=body))

        assert plan.suppression is not None
        assert plan.suppression.code is EnumCompanionSuppressionCode.HOLD_MARKER_TEXT
        assert plan.suppression.matched_text.lower() == "do not merge"
        assert plan.suppression.matched_location == "body"
        assert plan.suppression.matched_line == 5
        assert plan.suppression.evidence_lost is False
        # Naming the rule alone is what OMN-16665 rejects as unactionable.
        assert "do not merge" in plan.suppression.summary.lower()

    def test_hold_marker_in_the_title_reports_the_title(self) -> None:
        plan = compute_companion_plan(
            _request(pr_title="[WIP] feat(OMN-16669): the thing")
        )

        assert plan.suppression is not None
        assert plan.suppression.matched_location == "title"
        assert plan.suppression.matched_line == 1
        assert plan.suppression.matched_text.upper() == "WIP"

    def test_hold_label_reports_the_label(self) -> None:
        plan = compute_companion_plan(_request(pr_labels=("do-not-merge",)))

        assert plan.suppression is not None
        assert plan.suppression.code is EnumCompanionSuppressionCode.HOLD_LABEL
        assert plan.suppression.matched_location == "label"
        assert "do-not-merge" in plan.suppression.remediation

    def test_draft_reports_draft(self) -> None:
        plan = compute_companion_plan(_request(pr_is_draft=True))

        assert plan.suppression is not None
        assert plan.suppression.code is EnumCompanionSuppressionCode.PR_DRAFT
        assert plan.suppression.evidence_lost is False

    def test_no_ticket_reports_no_ticket(self) -> None:
        plan = compute_companion_plan(
            _request(pr_title="chore: tidy", pr_body="nothing cited")
        )

        assert plan.suppression is not None
        assert plan.suppression.code is EnumCompanionSuppressionCode.NO_TICKET

    def test_already_bound_reports_already_bound(self) -> None:
        plan = compute_companion_plan(
            _request(pr_body="Fixes OMN-16669.\n\nEvidence-Source: OCC#7200\n")
        )

        assert plan.suppression is not None
        assert plan.suppression.code is EnumCompanionSuppressionCode.ALREADY_BOUND

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"pr_is_draft": True}, id="draft"),
            pytest.param({"pr_labels": ("do-not-merge",)}, id="hold-label"),
            pytest.param({"pr_title": "[WIP] OMN-16669"}, id="hold-title"),
            pytest.param(
                {"pr_title": "chore: tidy", "pr_body": "none"}, id="no-ticket"
            ),
            pytest.param(
                {"pr_body": "OMN-16669\n\nEvidence-Source: OCC#7200\n"},
                id="already-bound",
            ),
            pytest.param(
                {"pr_state": "closed", "pr_merged": False}, id="closed-unmerged"
            ),
            pytest.param(
                {"pr_state": "closed", "pr_merged": True}, id="merged-unbound"
            ),
        ],
    )
    def test_no_declining_branch_is_silent(self, overrides: dict[str, object]) -> None:
        """The invariant, not the enumeration: a ``no_op`` plan with no
        ``suppression`` is un-surfaceable by construction, which is the exact
        shape this ticket exists to eliminate. A future declining branch added
        without a suppression fails here."""
        plan = compute_companion_plan(_request(**overrides))

        assert plan.no_op is True
        assert plan.suppression is not None
        assert plan.suppression.summary
        assert plan.suppression.remediation
