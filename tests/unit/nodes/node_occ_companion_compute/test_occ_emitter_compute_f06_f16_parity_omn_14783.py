# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14783 F-06 / F-16 cross-producer parity, restated for OMN-15247 R21.

There are two OCC-companion producers that must stay behaviorally equivalent on
the check_value fields F-06 and F-16 govern:

  * the LIVE born-path emitter — ``OccCompanionEmitter`` → the pure
    ``occ_evidence_stamp`` render family (``render_companion_contract`` /
    ``render_downstream_receipt`` / ``render_ci_check_receipt``); and
  * the canonical node graph — ``node_occ_state_effect`` (read) →
    ``node_occ_companion_compute`` (``compute_companion_plan``) →
    ``node_occ_companion_effect`` (write), via ``render_compute_companion_contract``
    / ``render_compute_receipt``.

This suite is a REAL cross-producer comparison — it drives BOTH producers' pure
render surfaces from ONE shared PR fact set and asserts they agree. It is not a
self-compare tautology.

WHAT CHANGED IN R21 (and why this file had to be rewritten rather than patched)
------------------------------------------------------------------------------
The original F-16 invariant was "for a PRIVATE product repo BOTH producers render
the declared check_values receipt-local (``grep -q '^status: PASS$'
$CONTRACT_REPO_DIR/drift/dod_receipts/...``)". That form is refused
UNCONDITIONALLY by the OMN-15309 admissibility predicate as INSIDE_OWN_DIFF — the
companion greps a receipt it authors in the same PR — and OCC's Contract
Compliance Check now enforces that predicate. Three consecutive machine-minted
companions (OCC#5406 / #5415 / #5418) were therefore born BLOCKED at 0-of-3
admissible. The old invariant was ASSERTING THE DEFECT, so it is replaced, not
relaxed:

  * F-06 (restated): the diff-scope check is the SAME admissible ``gh api
    .../pulls/${PR_NUMBER}/files`` assertion on BOTH producers, and NEITHER ships
    the REST-fragile ``gh pr diff ... --name-only`` (OCC#4297) NOR a bare
    ``gh pr view`` (NOT_EXECUTED under the predicate).
  * F-16 (restated): for a PRIVATE product repo NEITHER producer emits a declared
    contract ``check_value`` that NAMES the private repo, and NEITHER emits a
    self-referential receipt grep. The purpose F-16 was built for — "the hosted
    OCC runner has no token scope on the private repo" — is met by the
    placeholder form (``${REPO}`` / ``${PR_NUMBER}`` resolve to the repo/PR whose
    CI is executing), with no self-reference at all.

Each class carries a RED control proving the assertion is load-bearing
(feedback_prove_red_against_exists_but_wrong): every negative regex is proven to
match the exact pre-fix string it forbids.

Still deferred (NOT asserted here): whole-file byte-identity between the two
producers. They emit structurally different contracts by design — the emitter
declares two dod_evidence items with one check each; the compute declares one
item with two checks, plus the deploy-assessment / self-bind / merged-path
supersets.
"""

from __future__ import annotations

import re

import pytest
import yaml

from omnimarket.nodes.node_occ_companion_compute.handlers.handler_occ_companion_compute import (
    compute_companion_plan,
)
from omnimarket.nodes.node_occ_companion_compute.models.enum_companion_file_kind import (
    EnumCompanionFileKind,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_plan import (
    ModelOccCompanionPlan,
)
from omnimarket.nodes.node_occ_companion_compute.models.model_occ_companion_request import (
    ModelObservedProbe,
    ModelOccCompanionRequest,
)
from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (
    ci_check_evidence_id,
    downstream_receipt_public_check_value,
    hosted_safe_binding_check_value,
    hosted_safe_diff_scope_check_value,
    render_ci_check_receipt,
    render_companion_contract,
    render_downstream_receipt,
)

pytestmark = pytest.mark.unit

# --- Shared PR fact set (one canonical snapshot both producers consume) --------
TICKET = "OMN-9999"
PR = 321
HEAD_SHA = "b" * 40
TS = "2026-07-10T00:00:00Z"
BRANCH = "auto/occ-parity"
PUBLIC_REPO = "OmniNode-ai/omnimarket"
PRIVATE_REPO = "OmniNode-ai/omninode_infra"  # a real private OmniNode repo

# The admissible checks both producers must declare (verbatim, from the ONE home).
BINDING_CHECK = hosted_safe_binding_check_value()
DIFF_SCOPE_CHECK = hosted_safe_diff_scope_check_value()

# RED-control regexes. Proven load-bearing below (they DO match the pre-fix forms).
REST_DIFF_RE = re.compile(r"\bgh pr diff\b.*--name-only")
BARE_GH_PR_RE = re.compile(r"\bgh\s+pr\s+(?:view|diff|checks)\b")
SELF_REFERENTIAL_RE = re.compile(r"dod_receipts|\$\{?CONTRACT_REPO_DIR\b")


def _evidence_id(repo: str) -> str:
    # Both producers derive the evidence id by the SAME formula, so a shared repo
    # snapshot yields the same id on both sides (never hand-forked).
    return f"dod-{repo.replace('/', '-')}-pr-{PR}"


# --- Compute-side drivers ------------------------------------------------------
def _compute_plan(repo: str, *, private: bool) -> ModelOccCompanionPlan:
    request = ModelOccCompanionRequest(
        repo=repo,
        pr_number=PR,
        pr_head_sha=HEAD_SHA,
        pr_title="feat(OMN-9999): the thing",
        pr_body="Implements the thing.",
        run_timestamp=TS,
        product_probe=ModelObservedProbe(
            command=f"gh api repos/{repo}/pulls/{PR}/files",
            stdout='[{"filename":"src/x.py"}]',
            exit_code=0,
        ),
        product_repo_private=private,
    )
    return compute_companion_plan(request)


def _compute_contract_checks(repo: str, *, private: bool) -> list[str]:
    plan = _compute_plan(repo, private=private)
    contract = next(
        f for f in plan.companion_files if f.kind == EnumCompanionFileKind.CONTRACT
    )
    data = yaml.safe_load(contract.content)
    return [ck["check_value"] for item in data["dod_evidence"] for ck in item["checks"]]


def _compute_downstream_receipt(repo: str, *, private: bool) -> dict[str, object]:
    plan = _compute_plan(repo, private=private)
    receipt = next(
        f
        for f in plan.companion_files
        if f.kind == EnumCompanionFileKind.DOWNSTREAM_RECEIPT
    )
    return yaml.safe_load(receipt.content)


# --- Emitter-side drivers (the pure occ_evidence_stamp render family) ----------
def _emitter_contract_checks(repo: str, *, private: bool) -> list[str]:
    # R21: the private branch no longer swaps in a different value — ``None``
    # means "use the admissible defaults", which is what BOTH paths now do. The
    # ``private`` parameter is kept so the test still drives both call shapes.
    contract = render_companion_contract(
        ticket_id=TICKET,
        repo=repo,
        pr_number=PR,
        evidence_id=_evidence_id(repo),
        downstream_check_value=None,
        ci_check_value=None,
    )
    data = yaml.safe_load(contract)
    return [ck["check_value"] for item in data["dod_evidence"] for ck in item["checks"]]


def _emitter_downstream_receipt(repo: str, *, private: bool) -> dict[str, object]:
    text = render_downstream_receipt(
        ticket_id=TICKET,
        evidence_id=_evidence_id(repo),
        pr_number=PR,
        repo=repo,
        run_timestamp=TS,
        commit_sha=HEAD_SHA,
        branch=BRANCH,
        probe_command=f"gh api repos/{repo}/pulls/{PR}/files --paginate",
        probe_stdout='[{"filename":"src/x.py"}]',
        exit_code=0,
        check_value=None,
    )
    return yaml.safe_load(text)


def _emitter_ci_receipt(repo: str, *, private: bool) -> dict[str, object]:
    text = render_ci_check_receipt(
        ticket_id=TICKET,
        evidence_id=ci_check_evidence_id(_evidence_id(repo)),
        pr_number=PR,
        repo=repo,
        run_timestamp=TS,
        commit_sha=HEAD_SHA,
        branch=BRANCH,
        probe_command=f"gh api repos/{repo}/pulls/{PR}/files --paginate",
        probe_stdout='[{"status":"modified"}]',
        exit_code=0,
        check_value=None,
    )
    return yaml.safe_load(text)


# ---------------------------------------------------------------------------
# F-06 — one admissible diff-scope shape on both producers
# ---------------------------------------------------------------------------
class TestF06DiffScopeParity:
    def test_negative_regexes_are_load_bearing(self) -> None:
        # RED control: the negative assertions below are only meaningful if the
        # regexes actually match the exact pre-fix forms the producers shipped.
        assert REST_DIFF_RE.search(
            "gh pr diff ${PR_NUMBER} --repo ${REPO} --name-only | grep -q ."
        )
        assert BARE_GH_PR_RE.search(
            "gh pr view ${PR_NUMBER} --repo ${REPO} --json number,state"
        )
        assert BARE_GH_PR_RE.search(
            "gh pr view ${PR_NUMBER} --repo ${REPO} --json files"
        )

    def test_public_contract_diff_scope_is_gh_api_files_on_both_producers(self) -> None:
        emitter = _emitter_contract_checks(PUBLIC_REPO, private=False)
        compute = _compute_contract_checks(PUBLIC_REPO, private=False)
        assert DIFF_SCOPE_CHECK in emitter, emitter
        assert DIFF_SCOPE_CHECK in compute, compute

    def test_no_producer_ships_rest_fragile_or_bare_gh_pr(self) -> None:
        for repo, private in ((PUBLIC_REPO, False), (PRIVATE_REPO, True)):
            emitter = _emitter_contract_checks(repo, private=private)
            compute = _compute_contract_checks(repo, private=private)
            for cv in [*emitter, *compute]:
                assert not REST_DIFF_RE.search(cv), cv
                assert not BARE_GH_PR_RE.search(cv), cv

    def test_public_contract_check_value_sets_are_identical(self) -> None:
        # The strongest F-06 cross-producer statement: the two producers declare
        # the SAME set of contract check_values, even though they distribute them
        # across a different number of dod_evidence items.
        emitter = set(_emitter_contract_checks(PUBLIC_REPO, private=False))
        compute = set(_compute_contract_checks(PUBLIC_REPO, private=False))
        assert emitter == compute == {BINDING_CHECK, DIFF_SCOPE_CHECK}

    def test_public_downstream_receipt_binding_check_is_byte_equal(self) -> None:
        emitter = _emitter_downstream_receipt(PUBLIC_REPO, private=False)
        compute = _compute_downstream_receipt(PUBLIC_REPO, private=False)
        # The receipt records the LITERAL product-PR-pinned spelling of the same
        # binding the contract declares in placeholder form. Derived from the ONE
        # home rather than retyped: an assertion that hardcodes the string drifts
        # silently the moment the vocabulary is re-spelled (it did — this test was
        # left asserting the pre-anchor `--jq '.[].filename' | grep -q .` form).
        #
        # The anchor is load-bearing and MEASURED, not stylistic: on a 404 (a
        # nonexistent PR, or a token without scope on the repo) `gh api` writes
        # its ERROR BODY to STDOUT, and `--jq '.[].filename'` over that OBJECT
        # iterates its VALUES — so the unanchored `| grep -q .` exits 0 and
        # FALSE-GREENS on exactly the failure a cross-repo evidence check exists
        # to catch. `TestAnchorIsLoadBearing` below proves both directions.
        expected = downstream_receipt_public_check_value(pr_number=PR, repo=PUBLIC_REPO)
        assert emitter["check_value"] == compute["check_value"] == expected
        # Byte-level statement of the shape, so a silent re-spelling still fails.
        assert expected == (
            f"gh api repos/{PUBLIC_REPO}/pulls/{PR}/files --paginate "
            "--jq '.[].sha' | grep -qE '^[0-9a-f]{40}$'"
        )


# ---------------------------------------------------------------------------
# F-16 — private product repo: no cross-repo dereference, no self-reference
# ---------------------------------------------------------------------------
class TestF16PrivateRepoParity:
    def test_negative_regex_is_load_bearing(self) -> None:
        # RED control: proves SELF_REFERENTIAL_RE matches the exact pre-R21
        # private-repo form both producers used to emit.
        assert SELF_REFERENTIAL_RE.search(
            "grep -q '^status: PASS$' "
            "$CONTRACT_REPO_DIR/drift/dod_receipts/OMN-9999/dod-x/command.yaml"
        )

    def test_private_contract_names_no_private_repo(self) -> None:
        # The load-bearing R21 F-16 invariant: for a PRIVATE product repo NO
        # declared contract check_value dereferences the private repo by name, so
        # the hosted OCC runner (whose github.token has no scope on it — the
        # item-13 gap, OCC#5406) never needs that scope to execute the check.
        for cv in [
            *_emitter_contract_checks(PRIVATE_REPO, private=True),
            *_compute_contract_checks(PRIVATE_REPO, private=True),
        ]:
            assert PRIVATE_REPO not in cv, cv
            assert "${REPO}" in cv, cv

    def test_no_producer_emits_a_self_referential_check(self) -> None:
        # The defect this ticket removes: neither producer may declare a check
        # that reads back the receipt/contract tree it authors in the same PR.
        for repo, private in ((PUBLIC_REPO, False), (PRIVATE_REPO, True)):
            surface = [
                *_emitter_contract_checks(repo, private=private),
                str(_emitter_downstream_receipt(repo, private=private)["check_value"]),
                str(_emitter_ci_receipt(repo, private=private)["check_value"]),
                *_compute_contract_checks(repo, private=private),
                str(_compute_downstream_receipt(repo, private=private)["check_value"]),
            ]
            for cv in surface:
                assert not SELF_REFERENTIAL_RE.search(cv), f"self-reference: {cv}"

    def test_private_and_public_declare_the_same_admissible_checks(self) -> None:
        # R21 collapses the public/private fork in the CONTRACT: both render the
        # identical placeholder-form vocabulary. The public/private distinction
        # survives only where it is real — the LITERAL content-bound pin, which is
        # derived in handler_occ_state_effect under ``not product_repo_private``.
        assert (
            set(_compute_contract_checks(PRIVATE_REPO, private=True))
            == set(_compute_contract_checks(PUBLIC_REPO, private=False))
            == {BINDING_CHECK, DIFF_SCOPE_CHECK}
        )

    def test_private_receipt_preserves_live_probe_provenance(self) -> None:
        # F-16 does not erase provenance: the live cross-repo observation stays in
        # the receipt's probe_command on BOTH producers, naming the private repo,
        # because a receipt records what the producer ran — it is never re-executed
        # by the hosted runner (which executes the CONTRACT's declared value).
        compute = _compute_downstream_receipt(PRIVATE_REPO, private=True)
        emitter = _emitter_downstream_receipt(PRIVATE_REPO, private=True)
        for recv in (compute, emitter):
            probe = str(recv["probe_command"])
            assert PRIVATE_REPO in probe, probe


# ---------------------------------------------------------------------------
# R21 — the ANCHOR on each minted assertion is load-bearing, not cosmetic
# ---------------------------------------------------------------------------
class TestAnchorIsLoadBearing:
    """Every minted assertion must fail CLOSED when the API call fails.

    MEASURED against the live GitHub API while building OMN-15247 R21: on a 404
    — a nonexistent PR, or (the case that matters for a cross-repo evidence
    check) a token with no scope on the repo — ``gh api`` writes its ERROR BODY
    to **stdout**, not stderr::

        {"message":"Not Found","documentation_url":"https://...","status":"404"}

    ``--jq '.[].sha'`` over that OBJECT iterates its VALUES, so the pipeline
    emits ``Not Found`` / the doc URL / ``404``. An UNANCHORED terminal
    assertion (``| grep -q .``) therefore exits 0 and reports GREEN for a probe
    that never reached the repo — a false green produced by exactly the failure
    this evidence exists to detect.

    These tests are hermetic: they assert the terminal regex against the exact
    token stream a 404 body yields, so they need no network and no ``gh``.
    """

    # The values jq emits when iterating the 404 error object above.
    ERROR_BODY_VALUES = (
        "Not Found",
        "https://docs.github.com/rest/pulls/pulls#list-pull-requests-files",
        "404",
    )

    # Terminal anchors, extracted from the ONE home's rendered check_values.
    SHA_ANCHOR = re.compile(r"^[0-9a-f]{40}$")
    STATUS_ANCHOR = re.compile(r"^(added|modified|removed|renamed|changed|copied)$")

    def test_minted_values_carry_these_exact_anchors(self) -> None:
        # Binds the regexes below to the strings actually shipped, so a
        # re-spelling of the vocabulary cannot leave these tests asserting a
        # pattern the producer no longer emits.
        assert self.SHA_ANCHOR.pattern in hosted_safe_binding_check_value()
        assert self.STATUS_ANCHOR.pattern in hosted_safe_diff_scope_check_value()

    def test_no_error_body_value_satisfies_either_anchor(self) -> None:
        # RED direction: the 404 stdout stream must not satisfy any anchor.
        for value in self.ERROR_BODY_VALUES:
            assert not self.SHA_ANCHOR.match(value), value
            assert not self.STATUS_ANCHOR.match(value), value

    def test_unanchored_form_would_false_green_on_the_same_stream(self) -> None:
        # The RED CONTROL that makes the two tests above meaningful: prove the
        # rejected spelling (`| grep -q .`, i.e. "any non-empty line") DOES
        # accept the 404 stream. Without this, the assertions above could pass
        # against a stream nothing would ever accept.
        any_nonempty = re.compile(r".")
        for value in self.ERROR_BODY_VALUES:
            assert any_nonempty.search(value), value

    def test_real_api_shapes_satisfy_the_anchors(self) -> None:
        # GREEN direction: the shapes the API actually returns for a live PR.
        assert self.SHA_ANCHOR.match("0" * 40)
        assert self.SHA_ANCHOR.match("3b2adef4c0d1e2f3a4b5c6d7e8f9012345678abc")
        for status in ("added", "modified", "removed", "renamed", "changed", "copied"):
            assert self.STATUS_ANCHOR.match(status), status

    def test_anchors_reject_near_misses(self) -> None:
        # Falsifiability of the anchors themselves: a short sha, an uppercase
        # sha, and a status-like word outside GitHub's enum are all refused.
        assert not self.SHA_ANCHOR.match("0" * 39)
        assert not self.SHA_ANCHOR.match("0" * 41)
        assert not self.SHA_ANCHOR.match("A" * 40)
        assert not self.STATUS_ANCHOR.match("unchanged")
        assert not self.STATUS_ANCHOR.match("modified extra")
