# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for DurableEvidenceGate.

The gate refuses Linear Done transitions when the durable evidence trail in
``onex_change_control`` is local-only or cites a non-merged PR.

DEFAULT invocation (OMN-12593 config-drift fix)
-----------------------------------------------
The gate's default invocation resolves the receipt and contract against the
*real* control-plane layout: receipts at
``drift/dod_receipts/<TICKET>/<EVIDENCE_ITEM>/command.yaml`` (a directory, not a
single ``evidence/<TICKET>/dod_report.json`` file) and the dev-targeted OCC
governance ref ``origin/dev`` (not ``main``). The default-invocation regression
tests are ``test_default_invocation_passes_for_omn_12574_style_ticket`` and
``test_default_invocation_fails_when_receipt_truly_missing``.

Test surface (one test per failure mode + the pass case + helper functions):

1. ``test_untracked_receipt_hard_fails`` — no receipt under dir on dev → FAIL
2. ``test_contract_cites_superseded_pr_hard_fails`` — cited PR closed not merged
3. ``test_contract_cites_merged_pr_pass`` — citations match real merge SHAs
4. ``test_stale_occ_governance_ref_hard_fails`` — dev has older contract version
5. ``test_enforce_raises_with_structured_error`` — DurableEvidenceGateError
6. ``test_evaluate_no_citations_still_runs_other_checks``
7. ``test_extract_cited_merge_commits_handles_nested_checks``
8. ``test_parse_pr_url_handles_invalid``
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_dod_verify.models.model_durable_evidence_gate import (
    EnumDurableEvidenceCheck,
    EnumDurableEvidenceStatus,
)
from omnimarket.nodes.node_dod_verify.services.durable_evidence_gate import (
    DEFAULT_OCC_GOVERNANCE_REF,
    DurableEvidenceGate,
    DurableEvidenceGateError,
    default_contract_path,
    default_receipt_dir,
    extract_cited_merge_commits,
    parse_pr_url,
)

# Canonical default surfaces under test. These mirror the real OCC layout the
# platform writes (OccContractAdapter) and the dev-targeted governance ref.
_OCC_REPO = "/fake/onex_change_control"
_DEV_REF = DEFAULT_OCC_GOVERNANCE_REF  # "origin/dev"
_TICKET = "OMN-9855"
_RECEIPT_DIR = default_receipt_dir(_TICKET)  # drift/dod_receipts/OMN-9855
_CONTRACT_PATH = default_contract_path(_TICKET)  # contracts/OMN-9855.yaml


def _ticket_contract(
    *,
    pr_url: str = "https://github.com/OmniNode-ai/omnibase_core/pull/949",
    commit_sha: str = "abcdef1234567890abcdef1234567890abcdef12",
) -> dict[str, object]:
    """Build a contract dict with one cited (pr_url, commit_sha) pair."""
    return {
        "schema_version": "1.0.0",
        "ticket_id": "OMN-9855",
        "dod_evidence": [
            {
                "id": "dod-001",
                "description": "Code change shipped",
                "pr_url": pr_url,
                "commit_sha": commit_sha,
                "checks": [
                    {"check_type": "command", "check_value": "true"},
                ],
            }
        ],
    }


def _make_gate(
    *,
    tracked: dict[tuple[str, str, str], bool] | None = None,
    pr_view: dict[tuple[str, int], tuple[str, str | None]] | None = None,
    contract_on_main: dict[str, object] | None = None,
    occ_governance_ref: str = _DEV_REF,
) -> DurableEvidenceGate:
    """Build a DurableEvidenceGate with deterministic in-memory probe stubs.

    ``tracked`` is keyed by ``(repo_path, ref, receipt_dir)`` — the receipt-
    tracked probe answers whether ANY receipt is tracked under the ticket's
    receipt DIRECTORY (``drift/dod_receipts/<TICKET>``), not a single fixed
    receipt file.
    """
    tracked_map = tracked or {}
    pr_view_map = pr_view or {}

    def is_receipt_tracked(repo_path: str, ref: str, receipt_dir: str) -> bool:
        return tracked_map.get((repo_path, ref, receipt_dir), False)

    def gh_pr_view(repo: str, pr_number: int) -> tuple[str, str | None]:
        if (repo, pr_number) not in pr_view_map:
            msg = f"unexpected gh probe: {repo}#{pr_number}"
            raise AssertionError(msg)
        return pr_view_map[(repo, pr_number)]

    def load_contract(
        repo_path: str, ref: str, rel_path: str
    ) -> dict[str, object] | None:
        return contract_on_main

    return DurableEvidenceGate(
        is_receipt_tracked=is_receipt_tracked,
        gh_pr_view=gh_pr_view,
        load_contract_on_ref=load_contract,
        occ_repo_path=_OCC_REPO,
        occ_governance_ref=occ_governance_ref,
    )


@pytest.mark.unit
class TestDurableEvidenceGate:
    """Behaviour of the durable-evidence gate across pass/fail cases."""

    def test_untracked_receipt_hard_fails(self) -> None:
        """No receipt tracked under the receipt dir on dev = HARD FAIL."""
        contract = _ticket_contract()
        gate = _make_gate(
            tracked={
                # No receipt tracked under drift/dod_receipts/OMN-9855 on dev.
                (_OCC_REPO, _DEV_REF, _RECEIPT_DIR): False,
            },
            pr_view={
                ("OmniNode-ai/omnibase_core", 949): (
                    "MERGED",
                    "abcdef1234567890abcdef1234567890abcdef12",
                ),
            },
            contract_on_main=contract,
        )

        result = gate.evaluate(
            ticket_id=_TICKET,
            contract=contract,
            receipt_dir=_RECEIPT_DIR,
            contract_rel_path=_CONTRACT_PATH,
        )

        assert result.status == EnumDurableEvidenceStatus.FAIL
        receipt_check = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.RECEIPT_TRACKED
        )
        assert receipt_check.passed is False
        assert "No receipt is tracked" in receipt_check.message
        assert "Commit and push the command.yaml receipt" in receipt_check.message

    def test_contract_cites_superseded_pr_hard_fails(self) -> None:
        """Contract cites a non-merged PR (CLOSED/superseded) = HARD FAIL."""
        contract = _ticket_contract(
            pr_url="https://github.com/OmniNode-ai/omnibase_core/pull/926",
            commit_sha="b424155a89b298f85f04cd20016139b49d8877ed",
        )
        gate = _make_gate(
            tracked={
                (_OCC_REPO, _DEV_REF, _RECEIPT_DIR): True,
            },
            pr_view={
                # PR #926 was CLOSED-as-superseded, never merged.
                ("OmniNode-ai/omnibase_core", 926): ("CLOSED", None),
            },
            contract_on_main=contract,
        )

        result = gate.evaluate(
            ticket_id=_TICKET,
            contract=contract,
            receipt_dir=_RECEIPT_DIR,
            contract_rel_path=_CONTRACT_PATH,
        )

        assert result.status == EnumDurableEvidenceStatus.FAIL
        cite_check = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.CONTRACT_CITES_MERGE_COMMIT
        )
        assert cite_check.passed is False
        assert "state=CLOSED" in cite_check.message
        assert "expected MERGED" in cite_check.message

    def test_contract_cites_wrong_merge_sha_hard_fails(self) -> None:
        """PR is MERGED but mergeCommit.oid does not match cited SHA → FAIL."""
        contract = _ticket_contract(
            pr_url="https://github.com/OmniNode-ai/omnibase_core/pull/949",
            commit_sha="0000000000000000000000000000000000000000",
        )
        gate = _make_gate(
            tracked={
                (_OCC_REPO, _DEV_REF, _RECEIPT_DIR): True,
            },
            pr_view={
                ("OmniNode-ai/omnibase_core", 949): (
                    "MERGED",
                    "abcdef1234567890abcdef1234567890abcdef12",
                ),
            },
            contract_on_main=contract,
        )

        result = gate.evaluate(
            ticket_id=_TICKET,
            contract=contract,
            receipt_dir=_RECEIPT_DIR,
            contract_rel_path=_CONTRACT_PATH,
        )

        assert result.status == EnumDurableEvidenceStatus.FAIL
        cite_check = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.CONTRACT_CITES_MERGE_COMMIT
        )
        assert cite_check.passed is False
        assert "does not match" in cite_check.message
        assert "superseded or wrong PR" in cite_check.message

    def test_contract_cites_merged_pr_pass(self) -> None:
        """All checks green: tracked receipt + MERGED PR + dev matches → PASS."""
        contract = _ticket_contract()
        gate = _make_gate(
            tracked={
                (_OCC_REPO, _DEV_REF, _RECEIPT_DIR): True,
            },
            pr_view={
                ("OmniNode-ai/omnibase_core", 949): (
                    "MERGED",
                    "abcdef1234567890abcdef1234567890abcdef12",
                ),
            },
            contract_on_main=contract,
        )

        result = gate.evaluate(
            ticket_id=_TICKET,
            contract=contract,
            receipt_dir=_RECEIPT_DIR,
            contract_rel_path=_CONTRACT_PATH,
        )

        assert result.status == EnumDurableEvidenceStatus.PASS
        assert all(c.passed for c in result.checks)
        assert {c.check for c in result.checks} == {
            EnumDurableEvidenceCheck.RECEIPT_TRACKED,
            EnumDurableEvidenceCheck.CONTRACT_CITES_MERGE_COMMIT,
            EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN,
        }

    def test_stale_occ_governance_ref_hard_fails(self) -> None:
        """OCC dev still has stale contract (cites #926) while local cites #949 → FAIL."""
        # Local contract has been updated to cite the real merge commit (#949).
        local_contract = _ticket_contract(
            pr_url="https://github.com/OmniNode-ai/omnibase_core/pull/949",
            commit_sha="abcdef1234567890abcdef1234567890abcdef12",
        )
        # But OCC dev still has the older contract pointing at the superseded #926.
        stale_main_contract = _ticket_contract(
            pr_url="https://github.com/OmniNode-ai/omnibase_core/pull/926",
            commit_sha="b424155a89b298f85f04cd20016139b49d8877ed",
        )

        gate = _make_gate(
            tracked={
                (_OCC_REPO, _DEV_REF, _RECEIPT_DIR): True,
            },
            pr_view={
                ("OmniNode-ai/omnibase_core", 949): (
                    "MERGED",
                    "abcdef1234567890abcdef1234567890abcdef12",
                ),
            },
            contract_on_main=stale_main_contract,
        )

        result = gate.evaluate(
            ticket_id=_TICKET,
            contract=local_contract,
            receipt_dir=_RECEIPT_DIR,
            contract_rel_path=_CONTRACT_PATH,
        )

        assert result.status == EnumDurableEvidenceStatus.FAIL
        main_check = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN
        )
        assert main_check.passed is False
        assert "stale" in main_check.message
        assert "Open an OCC PR" in main_check.message

    def test_contract_missing_on_governance_ref_hard_fails(self) -> None:
        """Contract not yet present on the OCC governance ref → FAIL."""
        contract = _ticket_contract()
        gate = _make_gate(
            tracked={
                (_OCC_REPO, _DEV_REF, _RECEIPT_DIR): True,
            },
            pr_view={
                ("OmniNode-ai/omnibase_core", 949): (
                    "MERGED",
                    "abcdef1234567890abcdef1234567890abcdef12",
                ),
            },
            contract_on_main=None,
        )

        result = gate.evaluate(
            ticket_id=_TICKET,
            contract=contract,
            receipt_dir=_RECEIPT_DIR,
            contract_rel_path=_CONTRACT_PATH,
        )

        assert result.status == EnumDurableEvidenceStatus.FAIL
        main_check = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN
        )
        assert main_check.passed is False
        assert "not present" in main_check.message

    def test_enforce_raises_with_structured_error(self) -> None:
        """enforce() raises DurableEvidenceGateError carrying the result."""
        contract = _ticket_contract()
        gate = _make_gate(
            tracked={
                # No receipt tracked → first failure.
                (_OCC_REPO, _DEV_REF, _RECEIPT_DIR): False,
            },
            pr_view={
                ("OmniNode-ai/omnibase_core", 949): (
                    "MERGED",
                    "abcdef1234567890abcdef1234567890abcdef12",
                ),
            },
            contract_on_main=contract,
        )

        with pytest.raises(DurableEvidenceGateError) as exc_info:
            gate.enforce(
                ticket_id=_TICKET,
                contract=contract,
                receipt_dir=_RECEIPT_DIR,
                contract_rel_path=_CONTRACT_PATH,
            )

        err = exc_info.value
        assert err.result.ticket_id == _TICKET
        assert err.result.status == EnumDurableEvidenceStatus.FAIL
        assert "receipt_tracked" in str(err)

    def test_url_variants_across_local_and_ref_do_not_hard_fail(self) -> None:
        """Local contract spells the PR as ``/pull/123`` and the OCC governance
        ref spells it as ``/pull/123/files`` — same PR + same SHA, must PASS the
        CONTRACT_ON_OCC_MAIN check (regression for CR thread on PR #467).
        """
        local_contract = _ticket_contract(
            pr_url="https://github.com/OmniNode-ai/omnibase_core/pull/949",
            commit_sha="abcdef1234567890abcdef1234567890abcdef12",
        )
        main_contract = _ticket_contract(
            pr_url=("https://github.com/OmniNode-ai/omnibase_core/pull/949/files"),
            commit_sha="abcdef1234567890abcdef1234567890abcdef12",
        )

        gate = _make_gate(
            tracked={
                (_OCC_REPO, _DEV_REF, _RECEIPT_DIR): True,
            },
            pr_view={
                ("OmniNode-ai/omnibase_core", 949): (
                    "MERGED",
                    "abcdef1234567890abcdef1234567890abcdef12",
                ),
            },
            contract_on_main=main_contract,
        )

        result = gate.evaluate(
            ticket_id=_TICKET,
            contract=local_contract,
            receipt_dir=_RECEIPT_DIR,
            contract_rel_path=_CONTRACT_PATH,
        )

        assert result.status == EnumDurableEvidenceStatus.PASS
        main_check = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN
        )
        assert main_check.passed is True

    def test_evaluate_no_citations_still_runs_other_checks(self) -> None:
        """Contract with zero PR/commit citations passes the citation check
        but still enforces receipt-tracked + contract-on-ref.

        This mirrors the real OMN-12574 contract shape: the dod_evidence cites
        the receipt via a ``command`` check (no pr_url/commit_sha), so the
        citation check has nothing to verify, but receipt-tracked and
        contract-on-ref still gate the closure.
        """
        empty_contract: dict[str, object] = {
            "schema_version": "1.0.0",
            "ticket_id": "OMN-9999",
            "dod_evidence": [
                {
                    "id": "dod-001",
                    "description": "Documentation only",
                    "checks": [{"check_type": "file_exists", "path": "README.md"}],
                }
            ],
        }
        receipt_dir = default_receipt_dir("OMN-9999")
        gate = _make_gate(
            tracked={
                (_OCC_REPO, _DEV_REF, receipt_dir): True,
            },
            pr_view={},  # never invoked
            contract_on_main=empty_contract,
        )

        result = gate.evaluate(
            ticket_id="OMN-9999",
            contract=empty_contract,
            receipt_dir=receipt_dir,
            contract_rel_path=default_contract_path("OMN-9999"),
        )

        assert result.status == EnumDurableEvidenceStatus.PASS
        cite_check = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.CONTRACT_CITES_MERGE_COMMIT
        )
        assert "no PR/commit citations" in cite_check.message


@pytest.mark.unit
class TestDefaultInvocation:
    """DEFAULT-invocation regression tests for the OMN-12593 config-drift fix.

    The bug: the gate's defaults looked for the receipt at the speculative
    ``evidence/<TICKET>/dod_report.json`` and checked ``ref=main``, while the
    platform writes receipts at
    ``drift/dod_receipts/<TICKET>/<EVIDENCE_ITEM>/command.yaml`` and OCC
    governance is dev-targeted (contracts land on OCC ``dev``, batched to
    ``main`` later). The default invocation therefore FAILed
    (RECEIPT_TRACKED + CONTRACT_ON_OCC_MAIN) for tickets whose evidence was
    actually valid (e.g. OMN-12574 children).

    These tests pin the corrected default surfaces: the canonical defaults must
    PASS for an OMN-12574-style ticket and still FAIL fast when the receipt is
    truly missing.
    """

    def test_canonical_default_helpers(self) -> None:
        """The default path helpers and ref match the real platform layout."""
        assert DEFAULT_OCC_GOVERNANCE_REF == "origin/dev"
        assert default_receipt_dir("OMN-12574") == "drift/dod_receipts/OMN-12574"
        assert default_contract_path("OMN-12574") == "contracts/OMN-12574.yaml"

    def test_default_invocation_passes_for_omn_12574_style_ticket(self) -> None:
        """DEFAULT invocation PASSes for an OMN-12574-style ticket.

        Models the real OMN-12574 contract: its dod_evidence cites the receipt
        via a ``command`` check (no pr_url/commit_sha), the receipt lives at
        ``drift/dod_receipts/OMN-12574/<item>/command.yaml`` on OCC ``dev``, and
        the contract is present on OCC ``dev``. Under the DEFAULT invocation
        (canonical paths + dev-targeted ref) this MUST be a PASS — the pre-fix
        gate returned FAIL here, blocking valid closures.
        """
        ticket = "OMN-12574"
        # OMN-12574-shaped contract: receipt-verifying command check, no PR/SHA
        # citations (so CONTRACT_CITES_MERGE_COMMIT has nothing to verify).
        contract: dict[str, object] = {
            "schema_version": "1.0.0",
            "ticket_id": ticket,
            "dod_evidence": [
                {
                    "id": "dod-boundary-clone-retry-pr",
                    "description": "OCC PR #2083 retries validate-boundaries clones.",
                    "source": "manual",
                    "status": "verified",
                    "checks": [
                        {
                            "check_type": "command",
                            "check_value": (
                                "grep -q '^status: PASS$' "
                                "drift/dod_receipts/OMN-12574/"
                                "dod-boundary-clone-retry-pr/command.yaml"
                            ),
                        }
                    ],
                }
            ],
        }

        gate = _make_gate(
            tracked={
                # Receipt(s) ARE tracked under drift/dod_receipts/OMN-12574 on
                # the dev-targeted governance ref — the real platform layout.
                (
                    _OCC_REPO,
                    DEFAULT_OCC_GOVERNANCE_REF,
                    default_receipt_dir(ticket),
                ): True,
            },
            pr_view={},  # no PR/SHA citations → probe must never be invoked
            contract_on_main=contract,  # contract present on OCC dev
        )

        # DEFAULT invocation: caller passes only ticket_id + contract. The gate
        # resolves canonical paths and the dev ref internally.
        result = gate.evaluate_default(ticket_id=ticket, contract=contract)

        assert result.status == EnumDurableEvidenceStatus.PASS
        assert all(c.passed for c in result.checks)
        assert {c.check for c in result.checks} == {
            EnumDurableEvidenceCheck.RECEIPT_TRACKED,
            EnumDurableEvidenceCheck.CONTRACT_CITES_MERGE_COMMIT,
            EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN,
        }
        receipt_check = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.RECEIPT_TRACKED
        )
        # The PASS message references the canonical dir + dev ref, proving the
        # default resolution targeted the real layout, not the drifted one.
        assert "drift/dod_receipts/OMN-12574" in receipt_check.message
        assert "origin/dev" in receipt_check.message

    def test_default_invocation_fails_when_receipt_truly_missing(self) -> None:
        """DEFAULT invocation still FAILs fast when the receipt is truly absent.

        The fix must not weaken the gate's intent: with the contract present on
        OCC dev but NO receipt tracked under
        ``drift/dod_receipts/<TICKET>/`` on dev, the gate must HARD FAIL on
        RECEIPT_TRACKED — fail-fast on genuinely missing evidence, no silent
        fallback.
        """
        ticket = "OMN-12574"
        contract: dict[str, object] = {
            "schema_version": "1.0.0",
            "ticket_id": ticket,
            "dod_evidence": [
                {
                    "id": "dod-boundary-clone-retry-pr",
                    "description": "OCC PR #2083 retries validate-boundaries clones.",
                    "checks": [
                        {"check_type": "command", "check_value": "true"},
                    ],
                }
            ],
        }

        gate = _make_gate(
            tracked={
                # No receipt tracked anywhere on dev (truly missing evidence).
                (
                    _OCC_REPO,
                    DEFAULT_OCC_GOVERNANCE_REF,
                    default_receipt_dir(ticket),
                ): False,
            },
            pr_view={},
            contract_on_main=contract,
        )

        result = gate.evaluate_default(ticket_id=ticket, contract=contract)

        assert result.status == EnumDurableEvidenceStatus.FAIL
        receipt_check = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.RECEIPT_TRACKED
        )
        assert receipt_check.passed is False
        assert "No receipt is tracked" in receipt_check.message
        assert "drift/dod_receipts/OMN-12574" in receipt_check.message

    def test_enforce_default_raises_when_receipt_missing(self) -> None:
        """enforce_default() hard-fails (raises) when evidence is missing."""
        ticket = "OMN-12574"
        contract: dict[str, object] = {
            "schema_version": "1.0.0",
            "ticket_id": ticket,
            "dod_evidence": [{"id": "dod-001", "checks": [{"check_type": "command"}]}],
        }
        gate = _make_gate(
            tracked={
                (
                    _OCC_REPO,
                    DEFAULT_OCC_GOVERNANCE_REF,
                    default_receipt_dir(ticket),
                ): False,
            },
            pr_view={},
            contract_on_main=contract,
        )

        with pytest.raises(DurableEvidenceGateError) as exc_info:
            gate.enforce_default(ticket_id=ticket, contract=contract)

        assert exc_info.value.result.status == EnumDurableEvidenceStatus.FAIL
        assert "receipt_tracked" in str(exc_info.value)


@pytest.mark.unit
class TestExtractCitedMergeCommits:
    """extract_cited_merge_commits is the parser the gate relies on."""

    def test_top_level_pr_url_extracted(self) -> None:
        contract = {
            "dod_evidence": [
                {
                    "id": "dod-001",
                    "pr_url": "https://github.com/OmniNode-ai/omnibase_core/pull/949",
                    "commit_sha": "abc1234567890",
                }
            ]
        }
        cites = extract_cited_merge_commits(contract)
        assert len(cites) == 1
        assert cites[0].pr_number == 949
        assert cites[0].repo == "OmniNode-ai/omnibase_core"

    def test_extract_cited_merge_commits_handles_nested_checks(self) -> None:
        contract = {
            "dod_evidence": [
                {
                    "id": "dod-001",
                    "checks": [
                        {
                            "check_type": "command",
                            "pr_url": "https://github.com/OmniNode-ai/omnimarket/pull/123",
                            "commit_sha": "deadbeef1234567",
                        }
                    ],
                }
            ]
        }
        cites = extract_cited_merge_commits(contract)
        assert len(cites) == 1
        assert cites[0].pr_number == 123
        assert cites[0].repo == "OmniNode-ai/omnimarket"

    def test_dedupes_repeated_citations(self) -> None:
        contract = {
            "dod_evidence": [
                {
                    "pr_url": "https://github.com/OmniNode-ai/omnibase_core/pull/1",
                    "commit_sha": "aaa1111",
                },
                {
                    "pr_url": "https://github.com/OmniNode-ai/omnibase_core/pull/1",
                    "commit_sha": "aaa1111",
                },
            ]
        }
        cites = extract_cited_merge_commits(contract)
        assert len(cites) == 1

    def test_skips_malformed_entries(self) -> None:
        contract = {
            "dod_evidence": [
                {"id": "dod-001"},  # no pr_url/commit_sha
                {"pr_url": "not-a-url", "commit_sha": "abc"},
                {"pr_url": 42, "commit_sha": "abc"},  # type: ignore[dict-item]
            ]
        }
        cites = extract_cited_merge_commits(contract)
        assert cites == []

    def test_handles_non_list_dod_evidence(self) -> None:
        assert extract_cited_merge_commits({"dod_evidence": "not-a-list"}) == []
        assert extract_cited_merge_commits({}) == []

    def test_extract_skips_malformed_short_sha(self) -> None:
        """A valid pr_url paired with a malformed short SHA must be skipped,
        NOT raise ValidationError out of the extractor.

        Regression for CR thread on PR #467: previously the extractor accepted
        any string sha and let ``ModelCitedMergeCommit(min_length=7)`` raise.
        The docstring contracts that malformed citations are skipped.
        """
        contract = {
            "dod_evidence": [
                {
                    "id": "dod-malformed",
                    "pr_url": "https://github.com/OmniNode-ai/omnibase_core/pull/949",
                    "commit_sha": "abc",  # too short, must be skipped
                },
                {
                    "id": "dod-also-bad",
                    "pr_url": "https://github.com/OmniNode-ai/omnibase_core/pull/950",
                    "commit_sha": "zzzzzzz",  # right length, non-hex, must be skipped
                },
            ]
        }

        cites = extract_cited_merge_commits(contract)

        assert cites == []

    def test_url_variants_dedupe_to_same_citation(self) -> None:
        """``/pull/123`` and ``/pull/123/files`` for the same PR+SHA dedupe
        to a single citation.

        Regression for CR thread on PR #467: previously the extractor keyed
        dedupe on raw pr_url, so two URL variants for the same PR became
        two separate citations — double gh_pr_view() calls AND a false
        CONTRACT_ON_OCC_MAIN hard-fail when local and OCC main spell the
        same PR differently.
        """
        contract = {
            "dod_evidence": [
                {
                    "id": "dod-001",
                    "pr_url": "https://github.com/OmniNode-ai/omnibase_core/pull/123",
                    "commit_sha": "deadbeef1234567",
                },
                {
                    "id": "dod-002",
                    "pr_url": (
                        "https://github.com/OmniNode-ai/omnibase_core/pull/123/files"
                    ),
                    "commit_sha": "deadbeef1234567",
                },
            ]
        }

        cites = extract_cited_merge_commits(contract)

        assert len(cites) == 1
        assert cites[0].repo == "OmniNode-ai/omnibase_core"
        assert cites[0].pr_number == 123
        assert cites[0].cited_sha == "deadbeef1234567"


@pytest.mark.unit
class TestParsePrUrl:
    """parse_pr_url helper isolates the URL grammar."""

    def test_parses_canonical_pr_url(self) -> None:
        assert parse_pr_url(
            "https://github.com/OmniNode-ai/omnibase_core/pull/949"
        ) == ("OmniNode-ai/omnibase_core", 949)

    def test_parses_pr_url_with_trailing_path(self) -> None:
        assert parse_pr_url(
            "https://github.com/OmniNode-ai/omnimarket/pull/123/files"
        ) == ("OmniNode-ai/omnimarket", 123)

    def test_parse_pr_url_handles_invalid(self) -> None:
        assert parse_pr_url("not-a-url") is None
        assert parse_pr_url("https://github.com/owner/repo/issues/1") is None
        assert parse_pr_url("") is None
