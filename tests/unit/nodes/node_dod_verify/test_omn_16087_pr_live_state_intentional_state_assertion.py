# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-16087: the pr-live-state binder must not invert an intentional
non-merged PR-state assertion.

Failure mode: when a ``dod_evidence`` item's ``command`` check_value pins a
PR reference AND that same pipeline asserts a specific non-merged state for
it (``grep -qx OPEN`` / ``.state == "OPEN"`` / ``grep -qx 'CLOSED'``), the
auto-appended ``::pr-live-state`` check ignored the assertion and derived its
usual "must be MERGED and all required checks green" semantics from the bare
PR reference — inverting the entry's declared intent. A seam guard reading
"the pinned lineage deliberately predates unmerged PR #N" renders VERIFIED
only while #N stays unmerged, then flips to a false FAILURE the moment #N
merges; a "PR #N is CLOSED, replaced by #M" assertion renders a false
FAILURE immediately, because #N is (correctly) never merged at all.

Two live-discovered shapes, both reproduced here byte-for-byte from the real
contracts that hit this bug:

* ``contracts/OMN-16077.yaml`` item ``dod-pin-is-0386-head-predating-2736``
  (pre-supersession shape) — ``... --jq '.state' | grep -qx OPEN``.
* ``contracts/OMN-16142.yaml`` item ``occ-self-bind-pr-6624-superseded-note``
  — two same-item bindings, one asserting ``CLOSED`` (#6624) and one
  asserting ``MERGED`` (#6626, unaffected — the binder's default assumption
  already agrees with an explicit MERGED assertion and must still run its
  live merged/green derivation for that binding).

Fix under test: the binder scans each item's own check_value/command text for
a same-pipeline OPEN/CLOSED state assertion tied to a specific (repo,
pr_number) pin, and — for exactly that binding — emits SKIPPED instead of
deriving a merged/green judgement. The item's own declared ``command`` check
still executes and still verifies the asserted state directly (unaffected by
this fix), so nothing is weakened: a genuinely wrong assertion still FAILS
via the real check, just not via a second, inverted, auto-appended one.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumEvidenceCheckStatus,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

_TICKET = "OMN-99087"


def _install_fetch_mocks(
    collector: EvidenceCollector,
    *,
    merge_result: tuple[bool, str] | None,
    checks_result: tuple[bool, str],
) -> None:
    """Replace the two gh-shelling fetches with deterministic stubs.

    Mirrors the OMN-14207 test helper — any binding NOT excluded by this
    ticket's fix must still reach these fetches unchanged.
    """

    def _merge(repo: str, pr_number: int) -> tuple[bool, str] | None:
        return merge_result

    def _checks(repo: str, pr_number: int) -> tuple[bool, str]:
        return checks_result

    collector._fetch_pr_merge_state = _merge  # type: ignore[method-assign]
    collector._fetch_pr_checks_green = _checks  # type: ignore[method-assign]


@pytest.mark.unit
class TestOpenAssertionIsNotAutoBoundToMergedGreen:
    """Reproduces contracts/OMN-16077.yaml::dod-pin-is-0386-head-predating-2736
    (pre-supersession shape, AC3)."""

    _ITEM = {
        "id": "dod-pin-is-0386-head-predating-2736",
        "description": (
            "The pyproject [tool.uv.sources] rev at the PR head is exactly "
            "94247acffdab730105408d93c761986f4591cf55, and omnibase_infra#2736 "
            "was OPEN (unmerged) at bind time, so the pinned lineage cannot "
            "contain the revoked -> termination_reason rename."
        ),
        "checks": [
            {
                "check_type": "command",
                "check_value": (
                    "gh api repos/OmniNode-ai/omnimarket/contents/pyproject.toml"
                    "?ref=ea77ba914d8641740d960e828f22a4d45491557e --jq '.content' "
                    "| base64 -d | grep -c 'rev = "
                    '"94247acffdab730105408d93c761986f4591cf55"\' '
                    "| grep -qx 1 && gh pr view 2736 --repo OmniNode-ai/omnibase_infra "
                    "--json state --jq '.state' | grep -qx OPEN"
                ),
            }
        ],
    }

    def test_resolve_pr_bindings_still_derives_the_binding(self) -> None:
        """The binding itself is unaffected — only its downstream treatment
        changes. A regression here would silently defeat the exclusion path,
        which only fires for bindings that ARE resolved."""
        collector = EvidenceCollector()
        bindings = collector._resolve_pr_bindings(self._ITEM, _TICKET, None)
        assert bindings == [("OmniNode-ai/omnibase_infra", 2736)]

    def test_no_merged_green_binding_is_derived_for_the_open_assertion(self) -> None:
        """AC2: the derived check set contains no merged/green binding for
        the OPEN-asserted PR reference."""
        collector = EvidenceCollector()
        # If the exclusion fails, this mock would make the binding resolve
        # to VERIFIED (the old, inverted behaviour) instead of SKIPPED —
        # the test must fail loudly on that path, not pass by accident.
        _install_fetch_mocks(
            collector,
            merge_result=(True, "MERGED"),
            checks_result=(True, "all checks green"),
        )
        results = collector._live_pr_checks_for_item(self._ITEM, _TICKET, None)

        assert len(results) == 1
        result = results[0]
        assert (
            result.evidence_id == "dod-pin-is-0386-head-predating-2736::pr-live-state"
        )
        assert result.status == EnumEvidenceCheckStatus.SKIPPED, (
            "an intentional OPEN-assertion must not be auto-bound to "
            f"merged/green semantics; got status={result.status!r} "
            f"message={result.message!r}"
        )
        assert "OPEN" in (result.message or "")
        assert "2736" in (result.message or "")

    def test_verifies_with_the_intended_polarity_when_pr_is_genuinely_open(
        self,
    ) -> None:
        """AC3: reproduced as a fixture and verifies with the intended
        polarity — the live-state check must not itself render a FAILED
        verdict while #2736 is genuinely OPEN (the asserted, intended state).
        Pre-fix this rendered FAILED (PR not merged); post-fix it is SKIPPED,
        deferring to the item's own declared command check."""
        collector = EvidenceCollector()
        _install_fetch_mocks(
            collector,
            merge_result=(False, "OPEN"),
            checks_result=(True, "all checks green"),
        )
        results = collector._live_pr_checks_for_item(self._ITEM, _TICKET, None)

        assert len(results) == 1
        assert results[0].status != EnumEvidenceCheckStatus.FAILED

    def test_inverts_the_moment_the_referenced_pr_merges_pre_fix_evidence(
        self,
    ) -> None:
        """Documents the exact inversion this ticket closes: pre-fix, once
        #2736 merges, the OLD binder logic would derive VERIFIED for a
        binding whose own predicate says "must stay OPEN" — the wrong
        polarity. Post-fix the binding is SKIPPED regardless of #2736's live
        state, because the assertion is evaluated by the item's own command
        check, never by the auto-derived merged/green judgement."""
        collector = EvidenceCollector()
        _install_fetch_mocks(
            collector,
            merge_result=(True, "MERGED"),
            checks_result=(True, "all checks green"),
        )
        results = collector._live_pr_checks_for_item(self._ITEM, _TICKET, None)
        assert results[0].status == EnumEvidenceCheckStatus.SKIPPED


@pytest.mark.unit
class TestClosedReplacedAssertionIsNotAutoBoundToMergedGreen:
    """Reproduces contracts/OMN-16142.yaml::occ-self-bind-pr-6624-superseded-note
    (the 2026-08-18 comment's broadened scope: CLOSED/REPLACED, not only OPEN).

    This item asserts TWO PRs in one check_value: #6624 must be CLOSED
    (replaced) and #6626 must be MERGED. Only the #6624 binding is an
    intentional non-merged assertion; the #6626 binding must still receive
    the ordinary merged/green derivation.
    """

    _ITEM = {
        "id": "occ-self-bind-pr-6624-superseded-note",
        "description": (
            "PR #6624 was legitimately CLOSED (Receipt Gate rejected its "
            "branch name for not referencing OMN-16142) and its content "
            "re-landed as #6626, which is independently verified MERGED "
            "with all required contexts green."
        ),
        "evidence_artifact": "supersedes_dod_evidence:occ-self-bind-pr-6624",
        "checks": [
            {
                "check_type": "command",
                "check_value": (
                    "gh pr view 6624 --repo OmniNode-ai/onex_change_control "
                    "--json state --jq '.state' | grep -qx 'CLOSED' && "
                    "gh pr view 6626 --repo OmniNode-ai/onex_change_control "
                    "--json state --jq '.state' | grep -qx 'MERGED'"
                ),
            }
        ],
    }

    def test_both_bindings_are_still_resolved(self) -> None:
        collector = EvidenceCollector()
        bindings = collector._resolve_pr_bindings(self._ITEM, _TICKET, None)
        assert set(bindings) == {
            ("OmniNode-ai/onex_change_control", 6624),
            ("OmniNode-ai/onex_change_control", 6626),
        }

    def test_closed_asserted_pr_is_skipped_not_failed(self) -> None:
        """AC1 (broadened, 2026-08-18 comment): a bare PR reference whose own
        predicate asserts CLOSED must not be auto-bound to merged/green
        semantics either — same defect class as OPEN, opposite state."""
        collector = EvidenceCollector()
        # #6624 is genuinely CLOSED (never merged) — the old binder logic
        # would derive a hard FAILED here forever, which is exactly the
        # OMN-16142 closeout failure this fixture reproduces.
        _install_fetch_mocks(
            collector,
            merge_result=(False, "CLOSED"),
            checks_result=(False, "no required checks (PR closed)"),
        )
        results = collector._live_pr_checks_for_item(self._ITEM, _TICKET, None)

        by_id = {r.evidence_id: r for r in results}
        closed_id = "occ-self-bind-pr-6624-superseded-note::pr-6624-live-state"
        assert closed_id in by_id
        assert by_id[closed_id].status == EnumEvidenceCheckStatus.SKIPPED, (
            "an intentional CLOSED-assertion must not be auto-bound to "
            f"merged/green semantics; got {by_id[closed_id]!r}"
        )
        assert "CLOSED" in (by_id[closed_id].message or "")
        assert "6624" in (by_id[closed_id].message or "")

    def test_merged_asserted_pr_still_gets_the_real_live_derivation(self) -> None:
        """The fix must be surgical: #6626's MERGED assertion is exactly what
        the binder already assumes by default, and it still needs the real
        live probe — excluding it too would silently stop verifying the
        replacement PR ever actually merged (a genuine weakening this ticket
        must not introduce)."""
        collector = EvidenceCollector()
        _install_fetch_mocks(
            collector,
            merge_result=(True, "MERGED"),
            checks_result=(True, "all required checks green"),
        )
        results = collector._live_pr_checks_for_item(self._ITEM, _TICKET, None)

        by_id = {r.evidence_id: r for r in results}
        merged_id = "occ-self-bind-pr-6624-superseded-note::pr-6626-live-state"
        assert merged_id in by_id
        assert by_id[merged_id].status == EnumEvidenceCheckStatus.VERIFIED
        assert "MERGED" in (by_id[merged_id].message or "")

    def test_merged_asserted_pr_still_fails_when_actually_unmerged(self) -> None:
        """Non-vacuous check: if #6626 were NOT actually merged, the real
        live derivation for it must still FAIL — proving the fix did not
        blanket-skip the item's live checks."""
        collector = EvidenceCollector()
        _install_fetch_mocks(
            collector,
            merge_result=(False, "OPEN"),
            checks_result=(True, "all required checks green"),
        )
        results = collector._live_pr_checks_for_item(self._ITEM, _TICKET, None)

        by_id = {r.evidence_id: r for r in results}
        merged_id = "occ-self-bind-pr-6624-superseded-note::pr-6626-live-state"
        assert by_id[merged_id].status == EnumEvidenceCheckStatus.FAILED


@pytest.mark.unit
class TestBareUnassertedPrReferenceIsUnaffected:
    """A bare PR reference with no accompanying state predicate keeps the
    pre-existing (correct) behaviour: it still requires MERGED + green.
    The fix must not blanket-weaken every ``::pr-live-state`` check."""

    _ITEM = {
        "id": "dod-omnibase_infra-pr-2216",
        "description": "product PR #2216 carries the fix",
        "checks": [
            {
                "check_type": "command",
                "check_value": (
                    "gh pr view 2216 --repo OmniNode-ai/omnibase_infra "
                    "--json number,state"
                ),
            }
        ],
    }

    def test_no_state_assertion_still_requires_merged_and_green(self) -> None:
        collector = EvidenceCollector()
        _install_fetch_mocks(
            collector,
            merge_result=(False, "OPEN"),
            checks_result=(True, "all checks green"),
        )
        results = collector._live_pr_checks_for_item(self._ITEM, _TICKET, None)
        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.FAILED

    def test_no_state_assertion_verifies_when_merged_and_green(self) -> None:
        collector = EvidenceCollector()
        _install_fetch_mocks(
            collector,
            merge_result=(True, "MERGED"),
            checks_result=(True, "all checks green"),
        )
        results = collector._live_pr_checks_for_item(self._ITEM, _TICKET, None)
        assert len(results) == 1
        assert results[0].status == EnumEvidenceCheckStatus.VERIFIED
