# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-17943 — the retroactive diff-derived behavior-proof backfill.

What this covers, and why the refusals are the load-bearing half
----------------------------------------------------------------
``handler_evidence_autoclose_sweep.py`` returns an unconditional gap on
``if behavior_proving_count <= 0``, ahead of every other conjunct. That count
is a property of the CONTRACT, not of the work: a contract that declares no
behavior-class ``dod_evidence`` item is pinned at zero forever, however good
the merged code is.

The item ``dod-occ-diff-derived-behavior-proof`` first appears in OCC on
2026-08-28 and only 297 of 8,615 contracts declare it. Measured 2026-09-05
across the two beta sprint projects: 44 of 238 open tickets carry OCC receipts
and ZERO behavior-proof receipt. Those contracts predate the minter — they were
never judged and refused, they were born before the judge existed.

So the backfill applies the ALREADY-SHIPPED derivation retroactively. It is
deliberately not a new rule: ``derive_behavior_test_paths``,
``behavior_proof_check_value``, ``behavior_proof_cwd`` and
``render_behavior_proof_dod_evidence_item`` are imported from
``occ_evidence_stamp`` — the forward producer — so a backfilled item and a
forward-minted item cannot say different things about the same PR.

The tests that matter are the REFUSALS, because a backfill that mints
generously is a machine for manufacturing evidence:

* **A testless diff mints nothing.** The one thing on a product PR that is
  behavior proof by construction is the test the PR itself adds or changes.
  A PR with no such file has no derivable behavior proof, and inventing one —
  naming some other repo test, or a ``tests/`` file that is not a collection
  target — is exactly the class of check this ticket exists to remove.
* **A legacy whole-file receipt binding is untouchable.**
  ``check_receipt_hardening._contract_hash_violation`` validates
  ``contract_entry_sha256`` when present and falls back to the whole-file
  ``contract_sha256`` when it is not. Appending an item to a contract changes
  the whole-file hash, so backfilling a contract that carries a legacy
  hash-only receipt would INVALIDATE existing, merged evidence. Trading one
  gap for a broken binding is a loss, not a fix.
* **Status is derived, never assumed.** ``PASS`` requires a live readback
  saying the product PR merged and THE PR'S OWN CI — the check-runs on its head
  sha, not on the squash commit — concluded successfully, which is precisely
  what ``test_passes`` declares. Anything else is ``PENDING``, which is
  non-PASS and holds the ticket ineligible.
* **The minted item is byte-identical to the forward producer's.** Asserted by
  driving the real ``render_behavior_proof_dod_evidence_item`` over the same
  inputs, so drift between the two halves is a test failure rather than a
  silent divergence in the evidence corpus.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parents[3] / "scripts" / "ci"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import occ_behavior_proof_backfill as backfill  # noqa: E402

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_evidence_stamp import (  # noqa: E402
    BEHAVIOR_PROOF_EVIDENCE_ID,
    render_behavior_proof_dod_evidence_item,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Fixtures — a contract in the exact shape the autobind mints, minus the
# behavior-proof item. This is the real shape of the 44-ticket population:
# taken from OCC contracts/OMN-17872.yaml with the OMN-16434 item removed.
# ---------------------------------------------------------------------------

_CONTRACT_WITHOUT_BEHAVIOR_PROOF = """\
---
schema_version: "1.0.0"
ticket_id: "OMN-15425"
title: "Autobind OCC evidence for OMN-15425"
summary: "OCC Evidence-Source autobind companion for PR #3014."
is_seam_ticket: false
interface_change: false
interfaces_touched: []
evidence_requirements:
  - kind: "ci"
    description: "PR #3014 product diff scope present"
    command: "gh pr view ${PR_NUMBER} --repo ${REPO} --json files"
emergency_bypass:
  enabled: false
  justification: ""
  follow_up_ticket_id: ""
dod_evidence:
  - id: "dod-OmniNode-ai-omnibase_infra-pr-3014"
    description: "PR #3014 on OmniNode-ai/omnibase_infra — Evidence-Source autobind."
    source: "generated"
    checks:
      - check_type: "command"
        check_value: "gh pr view 3014 --repo OmniNode-ai/omnibase_infra --json number,state"
  - id: "dod-OmniNode-ai-omnibase_infra-pr-3014-ci"
    description: "PR #3014 on OmniNode-ai/omnibase_infra — product diff scope check."
    source: "generated"
    checks:
      - check_type: "command"
        check_value: "gh pr view 3014 --repo OmniNode-ai/omnibase_infra --json files"
"""

_MERGE_SHA = "5f2c0a1b9d3e4c6a7b8d9e0f1a2b3c4d5e6f7a8b"
_HEAD_SHA = "b7ba5802d9f66545b9c9ed9d762cf168ab5a4774"


def _pr_facts(
    *,
    state: str = "MERGED",
    checks_conclusion: str = "success",
    changed_files: tuple[str, ...] = (
        "src/omnibase_infra/thing.py",
        "tests/unit/test_thing_omn_15425.py",
    ),
) -> backfill.ProductPrFacts:
    return backfill.ProductPrFacts(
        repo="OmniNode-ai/omnibase_infra",
        pr_number=3014,
        state=state,
        merge_commit_sha=_MERGE_SHA if state == "MERGED" else "",
        head_sha=_HEAD_SHA,
        head_ref="jonah/omn-15425-thing",
        changed_files=changed_files,
        checks_conclusion=checks_conclusion,
        checks_probe_stdout='{"failing":0,"total":31}',
    )


def _receipts(*, legacy_whole_file_only: bool = False) -> dict[str, dict[str, Any]]:
    """Existing receipts on the contract, keyed by their repo-relative path."""
    body: dict[str, Any] = {
        "schema_version": "1.0.0",
        "ticket_id": "OMN-15425",
        "evidence_item_id": "dod-OmniNode-ai-omnibase_infra-pr-3014",
        "check_type": "command",
        "status": "PASS",
    }
    if legacy_whole_file_only:
        body["contract_sha256"] = "sha256:" + "a" * 64
    else:
        body["contract_entry_sha256"] = "sha256:" + "b" * 64
    return {
        "drift/dod_receipts/OMN-15425/dod-OmniNode-ai-omnibase_infra-pr-3014/"
        "command.yaml": body
    }


# ---------------------------------------------------------------------------
# AC1 — the refusal that keeps this from widening what counts as evidence.
# ---------------------------------------------------------------------------


def test_behavior_proof_backfill_refuses_a_testless_diff() -> None:
    """A merged PR whose diff carries no pytest target mints NOTHING.

    This is the test a naive implementation fails: minting for every ticket
    with a merged PR is the obvious shape, and it is the shape that fabricates
    behavior proof for work that never wrote a test.

    The assertion is deliberately made on the PLAN, not on a log line: the
    plan is what the writer consumes, so a decision that reports REFUSED while
    still carrying a mint would be caught here.
    """
    outcome = backfill.decide(
        ticket_id="OMN-15425",
        contract_text=_CONTRACT_WITHOUT_BEHAVIOR_PROOF,
        existing_receipts=_receipts(),
        behavior_receipt_exists=False,
        pr_facts=_pr_facts(
            changed_files=(
                "src/omnibase_infra/thing.py",
                "docs/runbooks/thing.md",
                # Under `tests/` but NOT a pytest collection target: naming it
                # would mint a command that collects nothing and passes
                # vacuously.
                "tests/conftest.py",
                "tests/fixtures/sample.json",
            )
        ),
    )

    assert (
        outcome.decision is backfill.EnumBackfillDecision.REFUSED_NO_BEHAVIOUR_IN_DIFF
    )
    assert outcome.test_paths == ()
    assert outcome.contract_item_text is None
    assert outcome.receipt_body is None
    assert "no pytest collection target" in outcome.reason


def test_a_tests_directory_file_that_is_not_a_collection_target_is_not_behaviour() -> (
    None
):
    """``tests/conftest.py`` alone is a refusal, not a mint.

    Guards the specific weakening a future maintainer is most likely to reach
    for — "it is under tests/, that is close enough" — which would restore
    vacuous collection.
    """
    outcome = backfill.decide(
        ticket_id="OMN-15425",
        contract_text=_CONTRACT_WITHOUT_BEHAVIOR_PROOF,
        existing_receipts=_receipts(),
        behavior_receipt_exists=False,
        pr_facts=_pr_facts(changed_files=("tests/conftest.py",)),
    )
    assert (
        outcome.decision is backfill.EnumBackfillDecision.REFUSED_NO_BEHAVIOUR_IN_DIFF
    )


# ---------------------------------------------------------------------------
# AC2 — appending must never invalidate evidence that already exists.
# ---------------------------------------------------------------------------


def test_a_legacy_whole_file_receipt_binding_refuses_the_append() -> None:
    """A contract carrying a hash-only receipt is left alone.

    ``_contract_hash_violation`` falls back to ``sha256(contract file)`` for a
    receipt with no ``contract_entry_sha256``. Appending a dod_evidence item
    changes that hash, so the append would turn a valid merged receipt into a
    "contract mutated after this receipt was produced" violation. The gap is
    real, but breaking existing evidence to close it is a net loss.
    """
    outcome = backfill.decide(
        ticket_id="OMN-15425",
        contract_text=_CONTRACT_WITHOUT_BEHAVIOR_PROOF,
        existing_receipts=_receipts(legacy_whole_file_only=True),
        behavior_receipt_exists=False,
        pr_facts=_pr_facts(),
    )

    assert (
        outcome.decision
        is backfill.EnumBackfillDecision.REFUSED_LEGACY_WHOLE_FILE_BINDING
    )
    assert outcome.contract_item_text is None
    assert "contract_sha256" in outcome.reason


def test_an_unmerged_pr_is_refused() -> None:
    """No merge, no derivation. There is no diff to be authoritative about."""
    outcome = backfill.decide(
        ticket_id="OMN-15425",
        contract_text=_CONTRACT_WITHOUT_BEHAVIOR_PROOF,
        existing_receipts=_receipts(),
        behavior_receipt_exists=False,
        pr_facts=_pr_facts(state="OPEN"),
    )
    assert outcome.decision is backfill.EnumBackfillDecision.REFUSED_PR_NOT_MERGED


def test_a_contract_that_already_declares_the_item_is_idempotent() -> None:
    """Re-running the backfill is a no-op, not a duplicate item."""
    already = _CONTRACT_WITHOUT_BEHAVIOR_PROOF + (
        f'  - id: "{BEHAVIOR_PROOF_EVIDENCE_ID}"\n'
        '    description: "already here"\n'
        '    source: "generated"\n'
        "    checks:\n"
        '      - check_type: "test_passes"\n'
        '        check_value: "uv run pytest tests/unit/test_thing.py -q"\n'
    )
    outcome = backfill.decide(
        ticket_id="OMN-15425",
        contract_text=already,
        existing_receipts=_receipts(),
        behavior_receipt_exists=True,
        pr_facts=_pr_facts(),
    )
    assert outcome.decision is backfill.EnumBackfillDecision.REFUSED_ALREADY_DECLARED


def test_a_contract_with_no_dod_evidence_block_is_refused_not_repaired() -> None:
    """Fail closed on an unexpected contract shape rather than guess a location."""
    shapeless = '---\nschema_version: "1.0.0"\nticket_id: "OMN-15425"\n'
    outcome = backfill.decide(
        ticket_id="OMN-15425",
        contract_text=shapeless,
        existing_receipts={},
        behavior_receipt_exists=False,
        pr_facts=_pr_facts(),
    )
    assert outcome.decision is backfill.EnumBackfillDecision.REFUSED_CONTRACT_SHAPE


# ---------------------------------------------------------------------------
# AC3 — the positive path, and its byte-for-byte agreement with the producer.
# ---------------------------------------------------------------------------


def test_backfill_mints_the_autobind_shape_byte_for_byte() -> None:
    """The minted item IS what the forward producer would emit today.

    Asserted against the real ``render_behavior_proof_dod_evidence_item``
    rather than a copied literal, so the two halves cannot drift: if the
    producer's rendering changes, this test changes with it or fails.
    """
    outcome = backfill.decide(
        ticket_id="OMN-15425",
        contract_text=_CONTRACT_WITHOUT_BEHAVIOR_PROOF,
        existing_receipts=_receipts(),
        behavior_receipt_exists=False,
        pr_facts=_pr_facts(),
    )

    assert outcome.decision is backfill.EnumBackfillDecision.MINT
    assert outcome.test_paths == ("tests/unit/test_thing_omn_15425.py",)

    expected_item = render_behavior_proof_dod_evidence_item(
        repo="OmniNode-ai/omnibase_infra",
        pr_number=3014,
        test_paths=("tests/unit/test_thing_omn_15425.py",),
    )
    assert outcome.contract_item_text == expected_item


def test_the_appended_contract_parses_and_carries_exactly_one_new_item() -> None:
    """The textual append produces valid YAML with the item in dod_evidence."""
    outcome = backfill.decide(
        ticket_id="OMN-15425",
        contract_text=_CONTRACT_WITHOUT_BEHAVIOR_PROOF,
        existing_receipts=_receipts(),
        behavior_receipt_exists=False,
        pr_facts=_pr_facts(),
    )
    assert outcome.contract_item_text is not None

    new_text = backfill.append_dod_evidence_item(
        _CONTRACT_WITHOUT_BEHAVIOR_PROOF, outcome.contract_item_text
    )
    data = yaml.safe_load(new_text)
    ids = [item["id"] for item in data["dod_evidence"]]
    assert ids == [
        "dod-OmniNode-ai-omnibase_infra-pr-3014",
        "dod-OmniNode-ai-omnibase_infra-pr-3014-ci",
        BEHAVIOR_PROOF_EVIDENCE_ID,
    ]
    entry = data["dod_evidence"][-1]
    assert entry["checks"][0]["check_type"] == "test_passes"
    assert (
        entry["checks"][0]["check_value"]
        == "uv run pytest tests/unit/test_thing_omn_15425.py -q"
    )
    assert entry["checks"][0]["cwd"] == "${OMNI_HOME}/omnibase_infra"

    # Every other key survives untouched — the append is an append.
    original = yaml.safe_load(_CONTRACT_WITHOUT_BEHAVIOR_PROOF)
    for key, value in original.items():
        if key == "dod_evidence":
            continue
        assert data[key] == value


def test_the_append_does_not_disturb_a_trailing_top_level_key() -> None:
    """A ``dod_evidence`` block in the MIDDLE of the file is handled.

    The autobind happens to emit it last; hand-authored contracts do not. An
    implementation that appended to end-of-file would corrupt those, and the
    corruption would be a YAML parse error on a governance artifact.
    """
    with_trailer = _CONTRACT_WITHOUT_BEHAVIOR_PROOF + 'notes: "trailing key"\n'
    item = '  - id: "x"\n    source: "generated"\n'
    data = yaml.safe_load(backfill.append_dod_evidence_item(with_trailer, item))
    assert data["notes"] == "trailing key"
    assert [entry["id"] for entry in data["dod_evidence"]][-1] == "x"


# ---------------------------------------------------------------------------
# AC3 — receipt honesty: status derived, entry-hash bound, never whole-file.
# ---------------------------------------------------------------------------


def test_the_minted_receipt_carries_an_entry_hash_and_never_a_whole_file_hash() -> None:
    """The backfill must not mint the very binding it refuses to break.

    A whole-file ``contract_sha256`` on a receipt is stale the moment anything
    else is appended to that contract. Minting one would seed the next
    generation of the exact refusal in this module.
    """
    receipt = backfill.build_backfill_receipt(
        ticket_id="OMN-15425",
        pr_facts=_pr_facts(),
        test_paths=("tests/unit/test_thing_omn_15425.py",),
        contract_entry_sha256="sha256:" + "c" * 64,
        status="PASS",
        run_url="https://github.com/OmniNode-ai/omnimarket/actions/runs/1",
    )
    assert "contract_sha256" not in receipt
    assert receipt["contract_entry_sha256"] == "sha256:" + "c" * 64
    assert receipt["evidence_item_id"] == BEHAVIOR_PROOF_EVIDENCE_ID
    assert receipt["check_type"] == "test_passes"
    assert receipt["commit_sha"] == _HEAD_SHA
    assert receipt["runner"] != receipt["verifier"]


def test_the_ci_readback_probes_the_pr_head_not_the_squash_commit() -> None:
    """``test_passes`` is about the PR's own CI, which lives on its head sha.

    MEASURED live on ``omninode_infra#1041`` while proving this mechanism: the
    head sha carried 30 success and nothing else, while the squash commit on
    ``dev`` carried 1 failure among 23 — a post-merge deploy workflow that ran
    after the code landed. Probing the merge commit marked three of four live
    candidates PENDING for reasons that had nothing to do with their tests.

    ``commit_sha`` is that SAME head sha (OMN-17943): the receipt field means
    "the code state this check ran against", and the check recorded here ran
    against the head. The merge commit is a different tree that no check in
    this receipt touched; ``actual_output`` names it as the separate fact it
    is, so nothing is lost by not binding to it.
    """
    receipt = backfill.build_backfill_receipt(
        ticket_id="OMN-15425",
        pr_facts=_pr_facts(),
        test_paths=("tests/unit/test_thing_omn_15425.py",),
        contract_entry_sha256="sha256:" + "c" * 64,
        status="PASS",
        run_url="https://github.com/OmniNode-ai/omnimarket/actions/runs/1",
    )
    assert _HEAD_SHA in receipt["probe_command"]
    assert _MERGE_SHA not in receipt["probe_command"]
    assert receipt["commit_sha"] == _HEAD_SHA
    assert _HEAD_SHA in receipt["actual_output"]
    assert _MERGE_SHA in receipt["actual_output"]


def test_receipt_status_is_derived_from_the_readback_not_assumed() -> None:
    """Merged-and-green is PASS; anything else is PENDING.

    PENDING is non-PASS, so a ticket whose merge checks did not conclude
    successfully stays ineligible — the backfill can widen the corpus of
    DECLARED bars without ever widening the corpus of PASSED ones.
    """
    assert backfill.derive_receipt_status(_pr_facts()) == "PASS"
    assert (
        backfill.derive_receipt_status(_pr_facts(checks_conclusion="failure"))
        == "PENDING"
    )
    assert backfill.derive_receipt_status(_pr_facts(checks_conclusion="")) == "PENDING"
    assert backfill.derive_receipt_status(_pr_facts(state="OPEN")) == "PENDING"


def test_a_pending_status_is_what_a_non_green_merge_actually_produces() -> None:
    """End-to-end on the decision, not only on the helper."""
    outcome = backfill.decide(
        ticket_id="OMN-15425",
        contract_text=_CONTRACT_WITHOUT_BEHAVIOR_PROOF,
        existing_receipts=_receipts(),
        behavior_receipt_exists=False,
        pr_facts=_pr_facts(checks_conclusion="failure"),
    )
    assert outcome.decision is backfill.EnumBackfillDecision.MINT
    assert outcome.receipt_status == "PENDING"


# ---------------------------------------------------------------------------
# Product-PR resolution — the contract already names its own PR.
# ---------------------------------------------------------------------------


def test_the_product_pr_is_read_out_of_the_contract_not_guessed() -> None:
    """The autobind writes ``dod-<owner>-<repo>-pr-<n>``; that IS the pointer.

    Reading it out of the contract avoids a second source of truth. The
    ``-ci`` sibling names the same PR and must not produce a second, competing
    answer.
    """
    data = yaml.safe_load(_CONTRACT_WITHOUT_BEHAVIOR_PROOF)
    ref = backfill.product_pr_from_contract(data)
    assert ref == backfill.ProductPrRef(
        repo="OmniNode-ai/omnibase_infra", pr_number=3014
    )


def test_a_contract_naming_no_product_pr_yields_no_reference() -> None:
    data = yaml.safe_load(
        '---\ndod_evidence:\n  - id: "occ-self-bind-pr-8236"\n    source: "generated"\n'
    )
    assert backfill.product_pr_from_contract(data) is None


def test_legacy_whole_file_receipts_are_detected_by_absence_not_by_name() -> None:
    """The predicate is 'no entry hash', which is what the validator branches on."""
    assert backfill.legacy_whole_file_receipts(_receipts()) == ()
    assert backfill.legacy_whole_file_receipts(
        _receipts(legacy_whole_file_only=True)
    ) == (
        "drift/dod_receipts/OMN-15425/dod-OmniNode-ai-omnibase_infra-pr-3014/"
        "command.yaml",
    )


# ---------------------------------------------------------------------------
# Candidate discovery — what a scheduled run works on when nobody names tickets.
# ---------------------------------------------------------------------------


def test_discovery_finds_receipted_tickets_missing_the_behaviour_item(
    tmp_path: Path,
) -> None:
    """Has receipts, no behavior proof, has a contract — all three required.

    A ticket with NO receipts at all is a different problem (no companion was
    ever bound) and is deliberately out of scope: minting a behavior item onto
    a contract nothing else references would produce an isolated bar with no
    surrounding evidence.
    """
    receipts = tmp_path / "drift" / "dod_receipts"
    contracts = tmp_path / "contracts"
    contracts.mkdir(parents=True)

    def _seed(ticket: str, *, behaviour: bool, receipted: bool, contract: bool) -> None:
        if receipted:
            (receipts / ticket / "dod-x").mkdir(parents=True, exist_ok=True)
            (receipts / ticket / "dod-x" / "command.yaml").write_text("---\n")
        else:
            (receipts / ticket).mkdir(parents=True, exist_ok=True)
        if behaviour:
            item = receipts / ticket / BEHAVIOR_PROOF_EVIDENCE_ID
            item.mkdir(parents=True, exist_ok=True)
            (item / "test_passes.yaml").write_text("---\n")
        if contract:
            (contracts / f"{ticket}.yaml").write_text("---\n")

    _seed("OMN-100", behaviour=False, receipted=True, contract=True)  # candidate
    _seed("OMN-300", behaviour=False, receipted=True, contract=True)  # candidate
    _seed("OMN-200", behaviour=True, receipted=True, contract=True)  # already done
    _seed("OMN-400", behaviour=False, receipted=False, contract=True)  # no receipts
    _seed("OMN-500", behaviour=False, receipted=True, contract=False)  # no contract

    assert backfill.discover_candidate_tickets(tmp_path, limit=10) == (
        "OMN-300",
        "OMN-100",
    )
    # Newest-first and bounded, so a limited run is resumable rather than
    # re-deciding the same head of the list every time.
    assert backfill.discover_candidate_tickets(tmp_path, limit=1) == ("OMN-300",)


def test_discovery_on_a_tree_with_no_receipts_yields_nothing(tmp_path: Path) -> None:
    """An empty result must come from an empty tree, not from a silent error.

    Paired with the test above, which is the positive control: the same call
    returns rows for a seeded tree, so a zero here is a measured zero.
    """
    assert backfill.discover_candidate_tickets(tmp_path, limit=10) == ()


# ---------------------------------------------------------------------------
# Repo resolution — both live check_value shapes, `--repo` winning ties.
# ---------------------------------------------------------------------------


def test_the_repo_is_recovered_from_a_gh_api_path_as_well_as_from_repo() -> None:
    """Both shapes appear in the live corpus and both must resolve.

    MEASURED on OCC dev across the 3,880 candidate contracts: 537 name the repo
    as `--repo <owner>/<name>`, and a further 277 name it ONLY inside a
    `gh api repos/<owner>/<name>/contents/...` path. Accepting only the first
    form refused those 277 as REFUSED_NO_PRODUCT_PR — fail-closed, so no wrong
    mint, but 34% of the resolvable corpus was unreachable for a PARSING reason
    rather than an evidentiary one. Found by the first live CI run of the
    scheduled workflow (17 of 25 discovered candidates refused that way).
    """
    assert (
        backfill.repo_from_check_value(
            "gh pr view 3014 --repo OmniNode-ai/omnibase_infra --json number,state"
        )
        == "OmniNode-ai/omnibase_infra"
    )
    assert (
        backfill.repo_from_check_value(
            "gh api repos/OmniNode-ai/omnimarket/contents/src/x.py?ref=abc "
            "--jq '.content'"
        )
        == "OmniNode-ai/omnimarket"
    )
    assert backfill.repo_from_check_value("uv run pytest tests/ -q") is None


def test_an_explicit_repo_flag_wins_over_an_api_path_in_the_same_check() -> None:
    """`--repo` is the explicit statement of where the PR lives.

    An api path in the same command may reference some OTHER repo's contents,
    so resolving to it would bind the behavior proof to the wrong repository —
    and `cwd` is derived from that repo name, so the minted check would run in
    a checkout that does not contain the test.
    """
    assert (
        backfill.repo_from_check_value(
            "gh api repos/OmniNode-ai/omnibase_core/contents/x.py "
            "&& gh pr view 1 --repo OmniNode-ai/omnimarket"
        )
        == "OmniNode-ai/omnimarket"
    )


def test_an_api_path_contract_now_resolves_end_to_end() -> None:
    """The refusal that motivated this becomes a resolution, on the real path."""
    contract = _CONTRACT_WITHOUT_BEHAVIOR_PROOF.replace(
        'check_value: "gh pr view 3014 --repo OmniNode-ai/omnibase_infra '
        '--json number,state"',
        'check_value: "gh api repos/OmniNode-ai/omnibase_infra/contents/'
        "src/omnibase_infra/thing.py?ref=abc --jq '.content'\"",
    )
    data = yaml.safe_load(contract)
    assert backfill.product_pr_from_contract(data) == backfill.ProductPrRef(
        repo="OmniNode-ai/omnibase_infra", pr_number=3014
    )


# ---------------------------------------------------------------------------
# OMN-17943 defect A — the generated receipt must satisfy the OCC Receipt
# Hardening Gate's repository-authority binding without a hand edit.
# ---------------------------------------------------------------------------

# Copied VERBATIM from onex_change_control
# `scripts/validation/check_receipt_hardening.py::_PRODUCT_REF_SHA_RE`, which is
# the gate that refused every mint this backfill produced.
#
# Vendored rather than imported on purpose, and the reason is worth stating
# because a reader will otherwise assume laziness: `onex_change_control` is not
# a dependency of omnimarket and is not importable here or in omnimarket CI
# (`uv run python -c "import onex_change_control"` fails on a synced tree), so
# there is no in-process handle on the real validator from this repo. What this
# test pins is therefore the RULE, restated from its source; the end-to-end
# proof that the real gate accepts these bytes is the gate's own verdict on the
# generated OCC PR, and the mirror of this assertion lives next to the
# validator in OCC so a change to the regex fails there.
_PRODUCT_REF_SHA_RE = re.compile(r"(?:/commits/|[?&]ref=)([0-9a-fA-F]{40})\b")


def _hardening_binding_violation(receipt: dict[str, Any]) -> str | None:
    """Re-statement of `_product_ref_binds_commit` over a generated receipt.

    The gate collects every full SHA exposed by a `/commits/<sha>` or
    `?ref=<sha>` segment of `check_value`/`probe_command` and requires the
    receipt's own `commit_sha` to be among them when the set is non-empty.
    """
    exposed = set()
    for field in ("check_value", "probe_command"):
        value = receipt.get(field)
        if isinstance(value, str):
            exposed.update(
                m.group(1).lower() for m in _PRODUCT_REF_SHA_RE.finditer(value)
            )
    if not exposed:
        return None
    if str(receipt["commit_sha"]).lower() in exposed:
        return None
    return (
        "[COMMIT_SHA_REPOSITORY] product command/ref exposes a full SHA that "
        f"does not bind receipt commit_sha; exposed={sorted(exposed)!r} "
        f"commit_sha={receipt['commit_sha']!r}"
    )


def test_the_generated_receipt_binds_the_commit_its_own_probe_ran_against() -> None:
    """The generated receipt passes the hardening binding rule, unedited.

    THIS IS THE REGRESSION. Before OMN-17943 the generator wrote
    `commit_sha = merge_commit_sha` while `probe_command` cited
    `/commits/<head_sha>/check-runs`, so every mint failed
    `[COMMIT_SHA_REPOSITORY]` and the only merged output (OCC#8344) needed a
    hand commit to land — a generator that cannot produce a landable artifact
    is a generator nobody can run unattended.

    Asserted as a violation-or-None so the failure message names both shas,
    which is what a reader needs to tell a binding bug from a fixture typo.
    """
    receipt = backfill.build_backfill_receipt(
        ticket_id="OMN-15425",
        pr_facts=_pr_facts(),
        test_paths=("tests/unit/test_thing_omn_15425.py",),
        contract_entry_sha256="sha256:" + "c" * 64,
        status="PASS",
        run_url="https://github.com/OmniNode-ai/omnimarket/actions/runs/1",
    )

    assert _hardening_binding_violation(receipt) is None


def test_the_binding_rule_restatement_actually_catches_the_old_shape() -> None:
    """Positive control for the check above.

    A predicate that returns None for everything would make the test above
    pass while proving nothing. Feed it the exact pre-fix shape — head sha in
    the probe, merge sha in `commit_sha` — and it must report the violation the
    live gate reported.
    """
    receipt = backfill.build_backfill_receipt(
        ticket_id="OMN-15425",
        pr_facts=_pr_facts(),
        test_paths=("tests/unit/test_thing_omn_15425.py",),
        contract_entry_sha256="sha256:" + "c" * 64,
        status="PASS",
        run_url="https://github.com/OmniNode-ai/omnimarket/actions/runs/1",
    )
    pre_fix = {**receipt, "commit_sha": _MERGE_SHA}

    violation = _hardening_binding_violation(pre_fix)
    assert violation is not None
    assert "COMMIT_SHA_REPOSITORY" in violation


# ---------------------------------------------------------------------------
# OMN-17943 defect B — a refused ticket must stop occupying the window.
# ---------------------------------------------------------------------------


def _seed_occ_tree(root: Path, ticket: str, *, contract_text: str) -> None:
    """One ticket with a receipt directory and a contract — a live candidate."""
    receipts = root / "drift" / "dod_receipts" / ticket / "dod-x"
    receipts.mkdir(parents=True, exist_ok=True)
    (receipts / "command.yaml").write_text(
        "---\ncontract_entry_sha256: 'sha256:" + "b" * 64 + "'\n", encoding="utf-8"
    )
    contracts = root / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    (contracts / f"{ticket}.yaml").write_text(contract_text, encoding="utf-8")


def _contract_naming_pr(ticket: str, repo: str, pr: int) -> str:
    return f"""\
---
schema_version: "1.0.0"
ticket_id: "{ticket}"
dod_evidence:
  - id: "dod-{repo.replace("/", "-")}-pr-{pr}"
    description: "PR #{pr} on {repo}."
    source: "generated"
    checks:
      - check_type: "command"
        check_value: "gh pr view {pr} --repo {repo} --json number,state"
"""


def test_a_ledgered_refusal_frees_its_window_slot_for_a_mintable_ticket(
    tmp_path: Path,
) -> None:
    """Three refused tickets ahead of one mintable one; the window yields the mintable one.

    This is the live shape of defect B, reproduced at fixture scale. On OCC dev
    on 2026-09-06 the newest-first top-40 window was FULLY occupied by refusals
    (22 testless diffs, 10 with no product PR, 8 legacy bindings) across 3,887
    candidates, and the nearest mintable ticket sat at rank 189 — unreachable
    by any number of scheduled runs, because a refusal never left the window.

    Discovery is newest-first, so the three refused ids are the HIGH numbers:
    without the ledger they would take every slot.
    """
    for ticket, pr in (("OMN-900", 1), ("OMN-800", 2), ("OMN-700", 3)):
        _seed_occ_tree(
            tmp_path,
            ticket,
            contract_text=_contract_naming_pr(ticket, "OmniNode-ai/omnimarket", pr),
        )
    _seed_occ_tree(
        tmp_path,
        "OMN-100",
        contract_text=_contract_naming_pr("OMN-100", "OmniNode-ai/omnimarket", 4),
    )

    ledger = {}
    for ticket, pr in (("OMN-900", 1), ("OMN-800", 2), ("OMN-700", 3)):
        ledger[ticket] = {
            "decision": "REFUSED_NO_BEHAVIOUR_IN_DIFF",
            "reason": "no pytest collection target in the merged diff.",
            "judged_at": "2026-09-06T00:00:00Z",
            "judged_against": {
                "product_prs": [f"OmniNode-ai/omnimarket#{pr}"],
                "legacy_binding_receipts": [],
            },
        }

    skipped: list[str] = []
    window = backfill.discover_candidate_tickets(
        tmp_path, limit=1, ledger=ledger, skipped=skipped
    )

    assert window == ("OMN-100",)
    assert skipped == ["OMN-900", "OMN-800", "OMN-700"]


def test_without_the_ledger_the_same_window_is_all_refusals(tmp_path: Path) -> None:
    """Positive control: the fixture really does starve the window unaided.

    Without this, the test above could pass because the tree was seeded wrong
    rather than because the ledger did anything.
    """
    for ticket, pr in (("OMN-900", 1), ("OMN-800", 2), ("OMN-700", 3)):
        _seed_occ_tree(
            tmp_path,
            ticket,
            contract_text=_contract_naming_pr(ticket, "OmniNode-ai/omnimarket", pr),
        )
    _seed_occ_tree(
        tmp_path,
        "OMN-100",
        contract_text=_contract_naming_pr("OMN-100", "OmniNode-ai/omnimarket", 4),
    )

    assert backfill.discover_candidate_tickets(tmp_path, limit=1) == ("OMN-900",)


def test_a_new_merged_product_pr_re_qualifies_a_ledgered_refusal(
    tmp_path: Path,
) -> None:
    """The skip lasts exactly as long as the facts it was judged against.

    A second consumer PR on the contract is a NEW merged diff, and a new diff
    can carry the pytest target the first one lacked. Nothing is written off
    permanently and no human has to remember to clear the entry.
    """
    contract = _contract_naming_pr("OMN-900", "OmniNode-ai/omnimarket", 1)
    _seed_occ_tree(tmp_path, "OMN-900", contract_text=contract)
    ledger = {
        "OMN-900": {
            "decision": "REFUSED_NO_BEHAVIOUR_IN_DIFF",
            "reason": "no pytest collection target in the merged diff.",
            "judged_at": "2026-09-06T00:00:00Z",
            "judged_against": {
                "product_prs": ["OmniNode-ai/omnimarket#1"],
                "legacy_binding_receipts": [],
            },
        }
    }

    assert backfill.discover_candidate_tickets(tmp_path, limit=5, ledger=ledger) == ()

    (tmp_path / "contracts" / "OMN-900.yaml").write_text(
        contract
        + """\
  - id: "dod-OmniNode-ai-omnimarket-pr-2"
    description: "PR #2 on OmniNode-ai/omnimarket."
    source: "generated"
    checks:
      - check_type: "command"
        check_value: "gh pr view 2 --repo OmniNode-ai/omnimarket --json number,state"
""",
        encoding="utf-8",
    )

    assert backfill.discover_candidate_tickets(tmp_path, limit=5, ledger=ledger) == (
        "OMN-900",
    )


def test_repairing_a_legacy_binding_re_qualifies_a_ledgered_refusal(
    tmp_path: Path,
) -> None:
    """`REFUSED_LEGACY_WHOLE_FILE_BINDING` clears when the binding is repaired.

    The repair is minting `contract_entry_sha256` onto the legacy receipt —
    the entry-hash-first branch of `_contract_hash_violation` then never
    reaches the whole-file fallback, so appending an item no longer restales
    it. The fingerprint notices with no manual step.
    """
    _seed_occ_tree(
        tmp_path,
        "OMN-900",
        contract_text=_contract_naming_pr("OMN-900", "OmniNode-ai/omnimarket", 1),
    )
    legacy = tmp_path / "drift" / "dod_receipts" / "OMN-900" / "dod-x" / "legacy.yaml"
    legacy.write_text(
        "---\ncontract_sha256: 'sha256:" + "a" * 64 + "'\n", encoding="utf-8"
    )
    legacy_rel = "drift/dod_receipts/OMN-900/dod-x/legacy.yaml"
    ledger = {
        "OMN-900": {
            "decision": "REFUSED_LEGACY_WHOLE_FILE_BINDING",
            "reason": "1 existing receipt(s) bind this contract by whole-file hash.",
            "judged_at": "2026-09-06T00:00:00Z",
            "judged_against": {
                "product_prs": ["OmniNode-ai/omnimarket#1"],
                "legacy_binding_receipts": [legacy_rel],
            },
        }
    }

    assert backfill.discover_candidate_tickets(tmp_path, limit=5, ledger=ledger) == ()

    legacy.write_text(
        "---\ncontract_sha256: 'sha256:"
        + "a" * 64
        + "'\ncontract_entry_sha256: 'sha256:"
        + "b" * 64
        + "'\n",
        encoding="utf-8",
    )

    assert backfill.discover_candidate_tickets(tmp_path, limit=5, ledger=ledger) == (
        "OMN-900",
    )


def test_a_run_records_its_refusals_and_writes_the_ledger_only_on_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dry run reports; an applied run persists. Never the other way round.

    Persisting from a dry run would let a REPORT change what the next run is
    allowed to see, which is the one property a dry run must not have.
    """
    _seed_occ_tree(
        tmp_path,
        "OMN-900",
        contract_text=_contract_naming_pr("OMN-900", "OmniNode-ai/omnimarket", 1),
    )
    # No network: the PR is unresolvable, which routes to REFUSED_NO_PRODUCT_PR
    # (a ledgered decision) rather than to a mint.
    monkeypatch.setattr(backfill, "resolve_pr_facts", lambda _ref: None)

    dry = backfill.run(
        occ_root=tmp_path,
        tickets=("OMN-900",),
        apply=False,
        run_url="",
        limit=5,
    )
    assert dry["refusal_ledger"]["recorded"] == 1
    assert dry["refusal_ledger"]["written"] is False
    assert not (tmp_path / backfill.REFUSAL_LEDGER_RELPATH).exists()

    applied = backfill.run(
        occ_root=tmp_path,
        tickets=("OMN-900",),
        apply=True,
        run_url="",
        limit=5,
    )
    assert applied["refusal_ledger"]["written"] is True

    reloaded = backfill.load_refusal_ledger(tmp_path)
    assert reloaded["OMN-900"]["decision"] == "REFUSED_NO_PRODUCT_PR"
    assert reloaded["OMN-900"]["judged_against"]["product_prs"] == [
        "OmniNode-ai/omnimarket#1"
    ]


def test_an_explicit_ticket_batch_is_never_suppressed_by_the_ledger(
    tmp_path: Path,
) -> None:
    """`--tickets` re-judges exactly what it is given.

    Naming a ticket explicitly IS the decision to re-judge it, so the operator
    batch path must not silently drop an id that discovery would have skipped —
    a batch that returns fewer rows than ids given is unreadable as evidence.
    """
    _seed_occ_tree(
        tmp_path,
        "OMN-900",
        contract_text=_contract_naming_pr("OMN-900", "OmniNode-ai/omnimarket", 1),
    )
    backfill.write_refusal_ledger(
        tmp_path,
        {
            "OMN-900": {
                "decision": "REFUSED_NO_BEHAVIOUR_IN_DIFF",
                "reason": "no pytest collection target.",
                "judged_at": "2026-09-06T00:00:00Z",
                "judged_against": {
                    "product_prs": ["OmniNode-ai/omnimarket#1"],
                    "legacy_binding_receipts": [],
                },
            }
        },
    )

    assert (
        backfill.discover_candidate_tickets(
            tmp_path, limit=5, ledger=backfill.load_refusal_ledger(tmp_path)
        )
        == ()
    )
    assert backfill._tickets_from("OMN-900") == ("OMN-900",)

    report = backfill.run(
        occ_root=tmp_path, tickets=("OMN-900",), apply=False, run_url="", limit=5
    )
    assert [row["ticket_id"] for row in report["outcomes"]] == ["OMN-900"]


# ---------------------------------------------------------------------------
# The ledger is committed into OCC, so it has to survive OCC's formatter.
# ---------------------------------------------------------------------------


def test_the_refusal_ledger_renders_yamlfmt_stable() -> None:
    """A plain PyYAML dump of this file fails OCC's yamlfmt hook every run.

    MEASURED on OCC#8493: the receipts rendered through the shared serializer
    passed untouched (they carry no lists), and yamlfmt rewrote ONLY the
    ledger — adding the `---` document start and indenting every block
    sequence one level under its key. OCC's `.yamlfmt` sets `indent: 2` and
    `include_document_start: true`; PyYAML emits neither by default.

    A generator whose output the destination repo reformats is a generator
    whose PR cannot land unattended, which is the same class of defect as the
    receipt binding this ticket already fixed.
    """
    rendered = backfill.render_refusal_ledger_yaml(
        {
            "refusals": {
                "OMN-900": {
                    "decision": "REFUSED_NO_BEHAVIOUR_IN_DIFF",
                    "judged_against": {
                        "legacy_binding_receipts": [],
                        "product_prs": ["OmniNode-ai/omnimarket#1"],
                    },
                }
            }
        }
    )

    assert rendered.startswith("---\n")
    assert "\n        - OmniNode-ai/omnimarket#1\n" in rendered
    # Never flush with the key — that is exactly what yamlfmt rewrites.
    assert "\n      - OmniNode-ai/omnimarket#1\n" not in rendered


def test_the_ledger_render_reloads_to_exactly_what_it_was_built_from() -> None:
    """Fail-closed, the same contract the receipt serializer carries.

    Bytes that do not reload to the object they were built from are never
    written: a ledger that says something other than what was judged would
    suppress the wrong tickets on the next discovery run. The guard runs on
    every render; this asserts the property it guards, and that an
    unrepresentable body is refused outright rather than written partially.
    """
    body = {
        "refusals": {
            "OMN-900": {
                "decision": "REFUSED_NO_BEHAVIOUR_IN_DIFF",
                "reason": "no pytest collection target in the merged diff.",
                "judged_against": {
                    "legacy_binding_receipts": ["drift/dod_receipts/OMN-900/x/y.yaml"],
                    "product_prs": ["OmniNode-ai/omnimarket#1"],
                },
            }
        }
    }

    assert yaml.safe_load(backfill.render_refusal_ledger_yaml(body)) == body

    with pytest.raises(yaml.YAMLError):
        backfill.render_refusal_ledger_yaml({"refusals": {"OMN-900": object()}})
