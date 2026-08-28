# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16440: an OCC-internal PR must never get its own OCC companion.

``compute_companion_plan`` treats every "product PR" the same, including one
that lives inside ``onex_change_control`` itself. That is a recursion trap, not
a content-binding gap: an OCC evidence PR's whole diff is ``contracts/*.yaml``
plus ``drift/dod_receipts/**/*.yaml``, so there is no code or test surface to
bind a check to, and the companion is minted anyway with a bare, non-falsifiable
existence probe (``gh pr view <n> --repo ... --json number,state,headRefName``)
on both the downstream item and the self-bind.

Live specimens: OCC#6927 was machine-minted as a "companion for
onex_change_control#6926" — a companion for a companion — and was closed as moot
by the discovering lane; OCC#6958 repeated it. Both carried only existence
probes, which pass whether or not the evidence they claim to prove exists.

This is the ticket's fix direction 2, the cheaper of the two it records, and the
one OMN-16434's landed producer fix (omnimarket#2180) explicitly did NOT close:
that PR added behaviour-class derivation from the PR's changed pytest targets,
and an OCC evidence PR has zero ``.py`` files, so the derivation returns empty
and the bare form still renders. No amount of better check derivation makes a
companion-for-a-companion meaningful, so the mint is declined at authoring time.

RED-first: every assertion below fails against the pre-fix
``compute_companion_plan``, which authors an OCC-internal companion happily.
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

_OCC_REPO = "OmniNode-ai/onex_change_control"
_PRODUCT_REPO = "OmniNode-ai/omnimemory"

# The live specimen: an OCC evidence PR that was itself companioned.
_OCC_PR = 6926


def _probe(repo: str, pr: int) -> ModelObservedProbe:
    return ModelObservedProbe(
        command=f"gh pr view {pr} --repo {repo} --json number,state",
        stdout=f'{{"number":{pr},"state":"OPEN"}}',
        exit_code=0,
    )


def _request(**overrides: object) -> ModelOccCompanionRequest:
    """A request that AUTHORS by default.

    Every suppression asserted below therefore has to come from the rule under
    test rather than from an inert fixture — the control at the top of this
    module proves the default mints.
    """
    repo = str(overrides.pop("repo", _PRODUCT_REPO))
    pr_number = int(overrides.pop("pr_number", _OCC_PR))  # type: ignore[call-overload]
    base: dict[str, object] = {
        "repo": repo,
        "pr_number": pr_number,
        "pr_head_sha": "b" * 40,
        "pr_title": "evidence(OMN-15911): OCC Evidence-Source autobind companion",
        "pr_body": "Evidence companion.\n\nSee OMN-15911 for the acceptance criteria.",
        "pr_state": "open",
        "pr_head_ref": "auto/omninode-ai-omnibase_infra-pr-2947-occ-autobind",
        # Non-trivial, non-infra changed files so the fast path cannot fire and
        # mask the decision under test.
        "changed_files": ("src/omnimemory/settings.py", "README.md"),
        "diff_total_lines": 9,
        "run_timestamp": "2026-08-28T10:00:00Z",
        "product_probe": _probe(repo, pr_number),
    }
    base.update(overrides)
    return ModelOccCompanionRequest(**base)  # type: ignore[arg-type]


@pytest.mark.unit
class TestControlTheRequestActuallyAuthors:
    def test_product_repo_pr_still_authors_a_companion(self) -> None:
        """Permanent negative control: the new rule must not become a blanket
        decline. A PR in a real product repo keeps minting exactly as before."""
        plan = compute_companion_plan(_request())

        assert plan.no_op is False
        assert plan.suppression is None
        assert plan.companion_files


@pytest.mark.unit
class TestOccInternalPrIsSuppressed:
    """The recursion trap: product repo IS the OCC repo."""

    def test_occ_internal_pr_is_declined(self) -> None:
        plan = compute_companion_plan(_request(repo=_OCC_REPO))

        assert plan.no_op is True
        assert plan.suppression is not None
        assert plan.suppression.code is EnumCompanionSuppressionCode.OCC_SELF_COMPANION
        assert plan.companion_files == ()

    def test_decline_is_not_an_evidence_hole(self) -> None:
        """``evidence_lost`` is reserved for a record that SHOULD exist and never
        will. An OCC evidence PR carries its own evidence in its own diff, so
        declining its companion loses nothing — it must not page anyone."""
        plan = compute_companion_plan(_request(repo=_OCC_REPO))

        assert plan.suppression is not None
        assert plan.suppression.evidence_lost is False

    def test_decline_names_the_repo_that_matched(self) -> None:
        """OMN-16665 AC2 parity: the surfaced text quotes what actually matched,
        so the decline is legible without reading this source file."""
        plan = compute_companion_plan(_request(repo=_OCC_REPO))

        assert plan.suppression is not None
        assert _OCC_REPO in plan.suppression.summary
        assert plan.suppression.matched_location == "repo"
        assert plan.suppression.matched_text == _OCC_REPO
        assert plan.suppression.remediation.strip()

    @pytest.mark.parametrize(
        "spelling",
        [
            "omninode-ai/onex_change_control",
            "OMNINODE-AI/ONEX_CHANGE_CONTROL",
            "  OmniNode-ai/onex_change_control  ",
        ],
    )
    def test_repo_match_is_case_and_whitespace_insensitive(self, spelling: str) -> None:
        """GitHub repo slugs are case-insensitive, and the seam carries whatever
        the caller wrote. A case difference must not reopen the trap."""
        plan = compute_companion_plan(_request(repo=spelling))

        assert plan.no_op is True
        assert plan.suppression is not None
        assert plan.suppression.code is EnumCompanionSuppressionCode.OCC_SELF_COMPANION


@pytest.mark.unit
class TestSuppressionIsKeyedOnTheRequestsOwnOccRepo:
    """The rule reads ``request.occ_repo``, not a hardcoded literal."""

    def test_overridden_occ_repo_suppresses_its_own_prs(self) -> None:
        plan = compute_companion_plan(
            _request(repo="OmniNode-ai/occ_fork", occ_repo="OmniNode-ai/occ_fork")
        )

        assert plan.no_op is True
        assert plan.suppression is not None
        assert plan.suppression.code is EnumCompanionSuppressionCode.OCC_SELF_COMPANION

    def test_default_occ_repo_does_not_suppress_when_occ_repo_is_overridden(
        self,
    ) -> None:
        """With the OCC repo pointed elsewhere, a PR in the DEFAULT OCC repo is
        an ordinary product PR for that deployment and still mints."""
        plan = compute_companion_plan(
            _request(repo=_OCC_REPO, occ_repo="OmniNode-ai/occ_fork")
        )

        assert plan.no_op is False
        assert plan.suppression is None


@pytest.mark.unit
class TestSuppressionOutranksTheMergedReplayOverride:
    """``allow_merged_replay`` (OMN-16665) exists to recover a companion the
    merge race destroyed. There is nothing to recover for an OCC-internal PR —
    the companion was never meaningful — so the recursion guard wins."""

    def test_merged_occ_pr_with_replay_override_is_still_declined(self) -> None:
        plan = compute_companion_plan(
            _request(
                repo=_OCC_REPO,
                pr_state="closed",
                pr_merged=True,
                allow_merged_replay=True,
            )
        )

        assert plan.no_op is True
        assert plan.suppression is not None
        assert plan.suppression.code is EnumCompanionSuppressionCode.OCC_SELF_COMPANION
        assert plan.suppression.evidence_lost is False

    def test_merged_product_pr_with_replay_override_still_recovers(self) -> None:
        """Negative control for the branch ordering: the recursion guard sits
        ahead of the merged-replay path but must not swallow it."""
        plan = compute_companion_plan(
            _request(pr_state="closed", pr_merged=True, allow_merged_replay=True)
        )

        assert plan.no_op is False
        assert plan.companion_files
