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
    assert receipt["commit_sha"] == _MERGE_SHA
    assert receipt["runner"] != receipt["verifier"]


def test_the_ci_readback_probes_the_pr_head_not_the_squash_commit() -> None:
    """``test_passes`` is about the PR's own CI, which lives on its head sha.

    MEASURED live on ``omninode_infra#1041`` while proving this mechanism: the
    head sha carried 30 success and nothing else, while the squash commit on
    ``dev`` carried 1 failure among 23 — a post-merge deploy workflow that ran
    after the code landed. Probing the merge commit marked three of four live
    candidates PENDING for reasons that had nothing to do with their tests.

    ``commit_sha`` stays the MERGE commit on purpose: that is the commit that
    exists on the default branch and that OCC's commit resolver can reach. The
    two shas answer two different questions and the receipt names both.
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
    assert receipt["commit_sha"] == _MERGE_SHA
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
