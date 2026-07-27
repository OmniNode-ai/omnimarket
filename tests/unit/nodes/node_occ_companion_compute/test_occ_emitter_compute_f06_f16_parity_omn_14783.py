# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14783 step 1 — F-06 / F-16 behavioral-parity between the two OCC producers.

There are two OCC-companion producers that must stay behaviorally equivalent on
the specific fields F-06 and F-16 govern:

  * the LIVE born-path emitter — ``OccCompanionEmitter`` → the pure
    ``occ_evidence_stamp`` render family (``render_companion_contract`` /
    ``render_downstream_receipt`` / ``render_ci_check_receipt``); and
  * the canonical node graph — ``node_occ_state_effect`` (read) →
    ``node_occ_companion_compute`` (``compute_companion_plan``) →
    ``node_occ_companion_effect`` (write), via ``render_compute_companion_contract``
    / ``render_compute_receipt``.

This suite is a REAL cross-producer comparison — it drives BOTH producers' pure
render surfaces from ONE shared PR fact set and asserts they agree on the F-06 and
F-16 fields. It is not a self-compare tautology.

Scope (deliberately narrow — the two producers emit STRUCTURALLY different
contracts by design: the emitter declares two dod_evidence items with one check
each; the compute declares one item with two checks, plus the compute's
deploy-assessment / self-bind / merged-path supersets). Whole-file byte-identity
is a LATER convergence stage and is NOT asserted here. This suite proves ONLY:

  * F-06 (OMN-14766): the product-diff-scope check is the GraphQL
    ``gh pr view ${PR_NUMBER} --repo ${REPO} --json files`` on BOTH producers,
    and NEITHER ships the REST-fragile ``gh pr diff ... --name-only`` (OCC#4297).
  * F-16 (OMN-14766): for a PRIVATE product repo BOTH producers render the
    declared check_values hosted-safe (receipt-local grep), so NEITHER re-runs a
    ``gh pr view --repo <private>`` the hosted OCC runner has no token scope for
    (OCC#4307/#4318) — while preserving the live probe as receipt provenance.

Each class carries a RED control proving the assertion is load-bearing
(feedback_prove_red_against_exists_but_wrong): the negative regexes are proven to
match the pre-fix forms, and the PUBLIC path is shown to still emit the hosted
forms the PRIVATE path strips (discriminator).

Deferred to step 2 (NOT asserted here): the receipt-structure difference — the
emitter mints a separate ``<evidence_id>-ci`` diff-scope receipt whose
``probe_command == check_value == gh pr view … --json files`` (D-06.2); the compute
folds the diff-scope claim into the single downstream item, so its private
diff-scope check points at the base item's receipt rather than a ``-ci`` receipt.
The load-bearing F-16 invariant (no hosted private probe) holds for both regardless.
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
    receipt_local_check_value,
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

# The F-06 GraphQL diff-scope check both producers must declare (verbatim).
F06_JSON_FILES = "gh pr view ${PR_NUMBER} --repo ${REPO} --json files"
# The public binding check both producers declare in the contract.
BINDING_JSON_STATE = "gh pr view ${PR_NUMBER} --repo ${REPO} --json number,state"

# RED-control regexes. Proven load-bearing below (they DO match the pre-fix forms).
REST_DIFF_RE = re.compile(r"\bgh pr diff\b.*--name-only")
HOSTED_RE = re.compile(r"\bgh\s+(?:pr\s+(?:view|diff|checks)|api)\b")


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
            command=f"gh pr view {PR} --repo {repo} --json number,state",
            stdout='{"number":321,"state":"OPEN"}',
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
    ev = _evidence_id(repo)
    if private:
        downstream_cv: str | None = receipt_local_check_value(
            ticket_id=TICKET, evidence_id=ev
        )
        ci_cv: str | None = receipt_local_check_value(
            ticket_id=TICKET, evidence_id=ci_check_evidence_id(ev)
        )
    else:
        downstream_cv = None
        ci_cv = None
    contract = render_companion_contract(
        ticket_id=TICKET,
        repo=repo,
        pr_number=PR,
        evidence_id=ev,
        downstream_check_value=downstream_cv,
        ci_check_value=ci_cv,
    )
    data = yaml.safe_load(contract)
    return [ck["check_value"] for item in data["dod_evidence"] for ck in item["checks"]]


def _emitter_downstream_receipt(repo: str, *, private: bool) -> dict[str, object]:
    ev = _evidence_id(repo)
    check_value = (
        receipt_local_check_value(ticket_id=TICKET, evidence_id=ev) if private else None
    )
    text = render_downstream_receipt(
        ticket_id=TICKET,
        evidence_id=ev,
        pr_number=PR,
        repo=repo,
        run_timestamp=TS,
        commit_sha=HEAD_SHA,
        branch=BRANCH,
        probe_command=f"gh pr view {PR} --repo {repo} --json number,state,headRefName",
        probe_stdout='{"number":321,"state":"OPEN"}',
        exit_code=0,
        check_value=check_value,
    )
    return yaml.safe_load(text)


def _emitter_ci_receipt(repo: str, *, private: bool) -> dict[str, object]:
    ev = _evidence_id(repo)
    check_value = (
        receipt_local_check_value(
            ticket_id=TICKET, evidence_id=ci_check_evidence_id(ev)
        )
        if private
        else None
    )
    text = render_ci_check_receipt(
        ticket_id=TICKET,
        evidence_id=ci_check_evidence_id(ev),
        pr_number=PR,
        repo=repo,
        run_timestamp=TS,
        commit_sha=HEAD_SHA,
        branch=BRANCH,
        probe_command=f"gh pr view {PR} --repo {repo} --json files",
        probe_stdout='{"number":321}',
        exit_code=0,
        check_value=check_value,
    )
    return yaml.safe_load(text)


# ---------------------------------------------------------------------------
# F-06 — GraphQL --json files diff-scope, no REST-fragile gh pr diff --name-only
# ---------------------------------------------------------------------------
class TestF06DiffScopeParity:
    def test_negative_regex_is_load_bearing(self) -> None:
        # RED control: the negative assertion below is only meaningful if the
        # regex actually matches the pre-F-06 form the compute contract shipped.
        assert REST_DIFF_RE.search(
            "gh pr diff ${PR_NUMBER} --repo ${REPO} --name-only | grep -q ."
        )

    def test_public_contract_diff_scope_is_json_files_on_both_producers(self) -> None:
        emitter = _emitter_contract_checks(PUBLIC_REPO, private=False)
        compute = _compute_contract_checks(PUBLIC_REPO, private=False)
        # A: the GraphQL diff-scope check is declared by BOTH producers.
        assert F06_JSON_FILES in emitter, emitter
        assert F06_JSON_FILES in compute, compute

    def test_public_no_producer_ships_rest_fragile_gh_pr_diff(self) -> None:
        emitter = _emitter_contract_checks(PUBLIC_REPO, private=False)
        compute = _compute_contract_checks(PUBLIC_REPO, private=False)
        # B: neither producer ships `gh pr diff ... --name-only` (was RED for the
        # compute before OMN-14783: its contract declared exactly that form).
        assert not any(REST_DIFF_RE.search(cv) for cv in emitter), emitter
        assert not any(REST_DIFF_RE.search(cv) for cv in compute), compute

    def test_public_contract_check_value_sets_are_identical(self) -> None:
        # The strongest F-06 cross-producer statement: on the public path the two
        # producers declare the SAME set of contract check_values (binding +
        # --json files diff-scope), even though they distribute them across a
        # different number of dod_evidence items.
        emitter = set(_emitter_contract_checks(PUBLIC_REPO, private=False))
        compute = set(_compute_contract_checks(PUBLIC_REPO, private=False))
        assert emitter == compute == {BINDING_JSON_STATE, F06_JSON_FILES}

    def test_public_downstream_receipt_binding_check_is_byte_equal(self) -> None:
        # The net-new downstream (binding) receipt check_value is byte-identical
        # across producers on the public path.
        emitter = _emitter_downstream_receipt(PUBLIC_REPO, private=False)
        compute = _compute_downstream_receipt(PUBLIC_REPO, private=False)
        expected = (
            f"gh pr view {PR} --repo {PUBLIC_REPO} --json number,state,headRefName"
        )
        assert emitter["check_value"] == compute["check_value"] == expected

    def test_emitter_ci_receipt_probe_equals_check_value(self) -> None:
        # OMN-14766 acceptance (emitter side): the born-path emitter mints a
        # dedicated --json files CI receipt whose probe_command == check_value.
        # The compute folds the diff-scope claim into the single downstream item
        # (receipt-structure convergence is deferred to step 2); this documents
        # the emitter's satisfaction of D-06.2 as the parity target.
        ci = _emitter_ci_receipt(PUBLIC_REPO, private=False)
        expected = f"gh pr view {PR} --repo {PUBLIC_REPO} --json files"
        assert ci["probe_command"] == ci["check_value"] == expected


# ---------------------------------------------------------------------------
# F-16 — private product repo → hosted-safe receipt-local check_values
# ---------------------------------------------------------------------------
class TestF16PrivateRepoParity:
    def test_hosted_regex_is_load_bearing(self) -> None:
        # RED control: the "no hosted private probe" assertion is only meaningful
        # if the regex actually matches a hosted gh pr view --repo <private> form.
        assert HOSTED_RE.search(
            f"gh pr view {PR} --repo {PRIVATE_REPO} --json number,state,headRefName"
        )

    def test_private_no_hosted_probe_in_either_producer(self) -> None:
        # The load-bearing F-16 invariant: for a PRIVATE product repo NO declared
        # check_value (contract item or receipt) re-runs a hosted gh pr/gh api
        # command the hosted OCC runner has no token scope for — asserted over
        # BOTH producers' full declared surface.
        emitter = [
            *_emitter_contract_checks(PRIVATE_REPO, private=True),
            str(_emitter_downstream_receipt(PRIVATE_REPO, private=True)["check_value"]),
            str(_emitter_ci_receipt(PRIVATE_REPO, private=True)["check_value"]),
        ]
        compute = [
            *_compute_contract_checks(PRIVATE_REPO, private=True),
            str(_compute_downstream_receipt(PRIVATE_REPO, private=True)["check_value"]),
        ]
        for cv in [*emitter, *compute]:
            assert not HOSTED_RE.search(cv), f"hosted private probe leaked: {cv}"

    def test_private_binding_check_is_byte_equal_across_producers(self) -> None:
        # The receipt-local binding check is byte-identical across producers: both
        # assert the SAME committed receipt attests PASS.
        expected = receipt_local_check_value(
            ticket_id=TICKET, evidence_id=_evidence_id(PRIVATE_REPO)
        )
        emitter = _emitter_contract_checks(PRIVATE_REPO, private=True)
        compute = _compute_contract_checks(PRIVATE_REPO, private=True)
        assert expected in emitter, emitter
        assert expected in compute, compute
        # And the compute's private downstream receipt check_value matches it too.
        recv = _compute_downstream_receipt(PRIVATE_REPO, private=True)
        assert recv["check_value"] == expected

    def test_private_receipt_preserves_live_probe_provenance(self) -> None:
        # F-16 does not erase provenance: the live gh pr view observation stays in
        # the receipt's probe_command on BOTH producers (only the re-runnable
        # check_value is swapped to the receipt-local form).
        compute = _compute_downstream_receipt(PRIVATE_REPO, private=True)
        emitter = _emitter_downstream_receipt(PRIVATE_REPO, private=True)
        for recv in (compute, emitter):
            probe = str(recv["probe_command"])
            assert "gh pr view" in probe, probe
            assert PRIVATE_REPO in probe, probe

    def test_public_path_emits_hosted_checks_discriminator(self) -> None:
        # Discriminator / RED control: the private path is proven to REMOVE hosted
        # checks that the PUBLIC path still emits — not to assert against something
        # that was never there. The public downstream receipt DOES carry a hosted
        # gh pr view, on both producers.
        compute = _compute_downstream_receipt(PUBLIC_REPO, private=False)
        emitter = _emitter_downstream_receipt(PUBLIC_REPO, private=False)
        assert HOSTED_RE.search(str(compute["check_value"]))
        assert HOSTED_RE.search(str(emitter["check_value"]))
