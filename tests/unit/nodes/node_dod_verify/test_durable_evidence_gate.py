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
2. ``test_contract_cites_superseded_pr_hard_fails`` — receipt-bound PR closed not merged
3. ``test_contract_cites_merged_pr_pass`` — receipt citations match real merge SHAs
4. ``test_stale_occ_governance_ref_hard_fails`` — dev has older contract version
5. ``test_enforce_raises_with_structured_error`` — DurableEvidenceGateError
6. ``test_zero_receipt_bound_commits_fails_citation_check``
7. ``test_receipt_json_url_extracted``
8. ``test_parse_pr_url_handles_invalid``
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_dod_verify.models.model_durable_evidence_gate import (
    EnumDefectLabel,
    EnumDoneClassLabel,
    EnumDurableEvidenceCheck,
    EnumDurableEvidenceStatus,
)
from omnimarket.nodes.node_dod_verify.services.durable_evidence_gate import (
    DEFAULT_OCC_GOVERNANCE_REF,
    DurableEvidenceGate,
    DurableEvidenceGateError,
    default_contract_path,
    default_receipt_dir,
    extract_receipt_merge_commits,
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
    evidence_id: str = "dod-001",
) -> dict[str, object]:
    """Build a schema-valid contract dict with one evidence check."""
    return {
        "schema_version": "1.0.0",
        "ticket_id": "OMN-9855",
        "dod_evidence": [
            {
                "id": evidence_id,
                "description": "Code change shipped",
                "checks": [
                    {"check_type": "command", "check_value": "true"},
                ],
            }
        ],
    }


def _receipt(
    *,
    repo: str = "OmniNode-ai/omnibase_core",
    pr_number: int = 949,
    commit_sha: str = "abcdef1234567890abcdef1234567890abcdef12",
    evidence_id: str = "dod-001",
    check_type: str = "command",
    status: str = "PASS",
) -> dict[str, object]:
    """Build a schema-valid receipt payload with a PR/commit binding."""
    return {
        "schema_version": "1.0.0",
        "ticket_id": _TICKET,
        "evidence_item_id": evidence_id,
        "check_type": check_type,
        "check_value": "true",
        "status": status,
        "run_timestamp": "2026-06-19T00:00:00Z",
        "commit_sha": commit_sha,
        "runner": "worker",
        "verifier": "reviewer",
        "probe_command": (
            f"gh pr view {pr_number} --repo {repo} --json number,url,state,mergeCommit"
        ),
        "probe_stdout": (
            f'{{"number":{pr_number},"url":"https://github.com/{repo}/pull/'
            f'{pr_number}","state":"MERGED","mergeCommit":{{"oid":"{commit_sha}"}}}}'
        ),
        "pr_number": pr_number,
    }


def _make_gate(
    *,
    tracked: dict[tuple[str, str, str], bool] | None = None,
    pr_view: dict[tuple[str, int], tuple[str, str | None]] | None = None,
    contract_on_main: dict[str, object] | None = None,
    receipts_on_ref: list[dict[str, object]] | None = None,
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

    def load_receipts(
        repo_path: str, ref: str, receipt_dir: str
    ) -> list[dict[str, object]]:
        return receipts_on_ref or []

    return DurableEvidenceGate(
        is_receipt_tracked=is_receipt_tracked,
        gh_pr_view=gh_pr_view,
        load_contract_on_ref=load_contract,
        load_receipts_on_ref=load_receipts,
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
        """Receipt cites a non-merged PR (CLOSED/superseded) = HARD FAIL."""
        contract = _ticket_contract()
        gate = _make_gate(
            tracked={
                (_OCC_REPO, _DEV_REF, _RECEIPT_DIR): True,
            },
            pr_view={
                # PR #926 was CLOSED-as-superseded, never merged.
                ("OmniNode-ai/omnibase_core", 926): ("CLOSED", None),
            },
            contract_on_main=contract,
            receipts_on_ref=[
                _receipt(
                    pr_number=926,
                    commit_sha="b424155a89b298f85f04cd20016139b49d8877ed",
                )
            ],
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
            receipts_on_ref=[
                _receipt(commit_sha="0000000000000000000000000000000000000000")
            ],
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
        assert "superseded, head-only, or wrong commit" in cite_check.message

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
            receipts_on_ref=[_receipt()],
        )

        result = gate.evaluate(
            ticket_id=_TICKET,
            contract=contract,
            receipt_dir=_RECEIPT_DIR,
            contract_rel_path=_CONTRACT_PATH,
            ticket_labels=frozenset({EnumDoneClassLabel.SOURCE_DONE.value}),
        )

        assert result.status == EnumDurableEvidenceStatus.PASS
        assert all(c.passed for c in result.checks)
        assert {c.check for c in result.checks} == {
            EnumDurableEvidenceCheck.RECEIPT_TRACKED,
            EnumDurableEvidenceCheck.CONTRACT_CITES_MERGE_COMMIT,
            EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN,
            EnumDurableEvidenceCheck.DEFECT_PREVENTION_GATE,
            EnumDurableEvidenceCheck.DONE_CLASS_LABEL,
        }

    def test_enforce_threads_done_class_labels(self) -> None:
        """enforce() passes label context through to the hard-fail gate."""
        contract = _ticket_contract()
        gate = _make_gate(
            tracked={(_OCC_REPO, _DEV_REF, _RECEIPT_DIR): True},
            pr_view={
                ("OmniNode-ai/omnibase_core", 949): (
                    "MERGED",
                    "abcdef1234567890abcdef1234567890abcdef12",
                ),
            },
            contract_on_main=contract,
            receipts_on_ref=[_receipt()],
        )

        result = gate.enforce(
            ticket_id=_TICKET,
            contract=contract,
            receipt_dir=_RECEIPT_DIR,
            contract_rel_path=_CONTRACT_PATH,
            ticket_labels=frozenset({EnumDoneClassLabel.SOURCE_DONE.value}),
        )

        done_class = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.DONE_CLASS_LABEL
        )
        assert done_class.passed is True

    def _all_green_gate(self) -> DurableEvidenceGate:
        """A gate whose durable checks all pass; label tests vary labels/contract."""
        return _make_gate(
            tracked={(_OCC_REPO, _DEV_REF, _RECEIPT_DIR): True},
            pr_view={
                ("OmniNode-ai/omnibase_core", 949): (
                    "MERGED",
                    "abcdef1234567890abcdef1234567890abcdef12",
                ),
            },
            contract_on_main=_ticket_contract(),
            receipts_on_ref=[_receipt()],
        )

    def test_plain_done_no_class_label_hard_fails(self) -> None:
        """Receipt+PR+contract all green but NO done-class label → HARD FAIL.

        OMN-13337 (retro R2): a plain Done with no approved done-class label is
        rejected at the gate boundary even when the three durable checks pass.
        """
        contract = _ticket_contract()
        gate = _make_gate(
            tracked={(_OCC_REPO, _DEV_REF, _RECEIPT_DIR): True},
            pr_view={
                ("OmniNode-ai/omnibase_core", 949): (
                    "MERGED",
                    "abcdef1234567890abcdef1234567890abcdef12",
                ),
            },
            contract_on_main=contract,
            receipts_on_ref=[_receipt()],
        )

        result = gate.evaluate(
            ticket_id=_TICKET,
            contract=contract,
            receipt_dir=_RECEIPT_DIR,
            contract_rel_path=_CONTRACT_PATH,
            # No done-class label supplied (default empty set).
        )

        assert result.status == EnumDurableEvidenceStatus.FAIL
        done_class = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.DONE_CLASS_LABEL
        )
        assert done_class.passed is False
        assert "no approved done-class label" in done_class.message
        # The three durable checks still pass — only the class check fails.
        assert all(
            c.passed
            for c in result.checks
            if c.check != EnumDurableEvidenceCheck.DONE_CLASS_LABEL
        )

    def test_non_done_class_label_does_not_satisfy(self) -> None:
        """A label outside the approved set does not count as a done-class."""
        contract = _ticket_contract()
        gate = _make_gate(
            tracked={(_OCC_REPO, _DEV_REF, _RECEIPT_DIR): True},
            pr_view={
                ("OmniNode-ai/omnibase_core", 949): (
                    "MERGED",
                    "abcdef1234567890abcdef1234567890abcdef12",
                ),
            },
            contract_on_main=contract,
            receipts_on_ref=[_receipt()],
        )

        result = gate.evaluate(
            ticket_id=_TICKET,
            contract=contract,
            receipt_dir=_RECEIPT_DIR,
            contract_rel_path=_CONTRACT_PATH,
            ticket_labels=frozenset({"bug", "needs-review"}),
        )

        assert result.status == EnumDurableEvidenceStatus.FAIL
        done_class = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.DONE_CLASS_LABEL
        )
        assert done_class.passed is False

    def test_done_class_label_without_tracked_receipt_hard_fails(self) -> None:
        """A done-class label not backed by a tracked receipt → HARD FAIL.

        The label alone is not evidence; it must be backed by RECEIPT_TRACKED.
        """
        contract = _ticket_contract()
        gate = _make_gate(
            tracked={(_OCC_REPO, _DEV_REF, _RECEIPT_DIR): False},
            pr_view={
                ("OmniNode-ai/omnibase_core", 949): (
                    "MERGED",
                    "abcdef1234567890abcdef1234567890abcdef12",
                ),
            },
            contract_on_main=contract,
            receipts_on_ref=[_receipt()],
        )

        result = gate.evaluate(
            ticket_id=_TICKET,
            contract=contract,
            receipt_dir=_RECEIPT_DIR,
            contract_rel_path=_CONTRACT_PATH,
            ticket_labels=frozenset({EnumDoneClassLabel.PROD_PROVEN.value}),
        )

        assert result.status == EnumDurableEvidenceStatus.FAIL
        done_class = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.DONE_CLASS_LABEL
        )
        assert done_class.passed is False
        assert "not evidence" in done_class.message

    def test_done_class_label_backed_by_receipt_passes(self) -> None:
        """An approved done-class label backed by a tracked receipt → PASS."""
        contract = _ticket_contract()
        gate = _make_gate(
            tracked={(_OCC_REPO, _DEV_REF, _RECEIPT_DIR): True},
            pr_view={
                ("OmniNode-ai/omnibase_core", 949): (
                    "MERGED",
                    "abcdef1234567890abcdef1234567890abcdef12",
                ),
            },
            contract_on_main=contract,
            receipts_on_ref=[_receipt()],
        )

        result = gate.evaluate(
            ticket_id=_TICKET,
            contract=contract,
            receipt_dir=_RECEIPT_DIR,
            contract_rel_path=_CONTRACT_PATH,
            ticket_labels=frozenset({EnumDoneClassLabel.PROJECTION_BACKED.value}),
        )

        assert result.status == EnumDurableEvidenceStatus.PASS
        done_class = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.DONE_CLASS_LABEL
        )
        assert done_class.passed is True
        assert EnumDoneClassLabel.PROJECTION_BACKED.value in done_class.message

    def test_done_class_label_values_are_the_six_canonical(self) -> None:
        """The approved done-class set is exactly the six canonical labels."""
        assert EnumDoneClassLabel.values() == frozenset(
            {
                "source-done",
                "runtime-observed",
                "projection-backed",
                "replay-proven",
                "demo-visible",
                "prod-proven",
            }
        )

    def test_defect_ticket_without_prevention_hard_fails(self) -> None:
        """A defect-labelled ticket with no prevention gate / note -> HARD FAIL."""
        contract = _ticket_contract()
        gate = self._all_green_gate()

        result = gate.evaluate(
            ticket_id=_TICKET,
            contract=contract,
            receipt_dir=_RECEIPT_DIR,
            contract_rel_path=_CONTRACT_PATH,
            ticket_labels=frozenset(
                {EnumDefectLabel.BUG.value, EnumDoneClassLabel.SOURCE_DONE.value}
            ),
        )

        assert result.status == EnumDurableEvidenceStatus.FAIL
        defect = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.DEFECT_PREVENTION_GATE
        )
        assert defect.passed is False
        assert "cannot close" in defect.message
        assert all(
            c.passed
            for c in result.checks
            if c.check != EnumDurableEvidenceCheck.DEFECT_PREVENTION_GATE
        )

    def test_defect_ticket_with_prevention_gate_passes(self) -> None:
        """A defect ticket that links a prevention gate -> PASS."""
        contract = dict(_ticket_contract())
        contract["prevention_gate"] = (
            ".github/workflows/validator-no-except-swallow.yml"
        )
        gate = self._all_green_gate()

        result = gate.evaluate(
            ticket_id=_TICKET,
            contract=contract,
            receipt_dir=_RECEIPT_DIR,
            contract_rel_path=_CONTRACT_PATH,
            ticket_labels=frozenset(
                {
                    EnumDefectLabel.REGRESSION.value,
                    EnumDoneClassLabel.PROJECTION_BACKED.value,
                }
            ),
        )

        assert result.status == EnumDurableEvidenceStatus.PASS
        defect = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.DEFECT_PREVENTION_GATE
        )
        assert defect.passed is True
        assert "prevention_gate" in defect.message

    def test_defect_ticket_with_non_recurrence_note_passes(self) -> None:
        """A defect ticket that carries a non-recurrence note -> PASS."""
        contract = dict(_ticket_contract())
        contract["non_recurrence_note"] = (
            "One-off data-entry typo in a fixture; no code path can reproduce it, "
            "so no automated gate is feasible."
        )
        gate = self._all_green_gate()

        result = gate.evaluate(
            ticket_id=_TICKET,
            contract=contract,
            receipt_dir=_RECEIPT_DIR,
            contract_rel_path=_CONTRACT_PATH,
            ticket_labels=frozenset(
                {
                    EnumDefectLabel.DEFECT.value,
                    EnumDoneClassLabel.RUNTIME_OBSERVED.value,
                }
            ),
        )

        assert result.status == EnumDurableEvidenceStatus.PASS
        defect = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.DEFECT_PREVENTION_GATE
        )
        assert defect.passed is True
        assert "non_recurrence_note" in defect.message

    def test_blank_prevention_fields_do_not_satisfy(self) -> None:
        """Whitespace-only prevention fields are treated as absent -> HARD FAIL."""
        contract = dict(_ticket_contract())
        contract["prevention_gate"] = "   "
        contract["non_recurrence_note"] = ""
        gate = self._all_green_gate()

        result = gate.evaluate(
            ticket_id=_TICKET,
            contract=contract,
            receipt_dir=_RECEIPT_DIR,
            contract_rel_path=_CONTRACT_PATH,
            ticket_labels=frozenset(
                {EnumDefectLabel.BUG.value, EnumDoneClassLabel.SOURCE_DONE.value}
            ),
        )

        assert result.status == EnumDurableEvidenceStatus.FAIL
        defect = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.DEFECT_PREVENTION_GATE
        )
        assert defect.passed is False

    def test_non_defect_ticket_is_exempt_from_ratchet(self) -> None:
        """A non-defect ticket passes the ratchet check N/A even with no fields."""
        contract = _ticket_contract()
        gate = self._all_green_gate()

        result = gate.evaluate(
            ticket_id=_TICKET,
            contract=contract,
            receipt_dir=_RECEIPT_DIR,
            contract_rel_path=_CONTRACT_PATH,
            ticket_labels=frozenset({EnumDoneClassLabel.DEMO_VISIBLE.value}),
        )

        assert result.status == EnumDurableEvidenceStatus.PASS
        defect = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.DEFECT_PREVENTION_GATE
        )
        assert defect.passed is True
        assert "does not apply" in defect.message

    def test_defect_label_values_are_canonical(self) -> None:
        """The defect-class label set is exactly the three canonical labels."""
        assert EnumDefectLabel.values() == frozenset({"bug", "defect", "regression"})

    def test_stale_occ_governance_ref_hard_fails(self) -> None:
        """OCC dev still has a stale contract missing the receipt-bound check."""
        local_contract = _ticket_contract(evidence_id="dod-949")
        stale_main_contract = _ticket_contract(evidence_id="dod-926")

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
            receipts_on_ref=[_receipt(evidence_id="dod-949")],
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
            receipts_on_ref=[_receipt()],
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
            receipts_on_ref=[_receipt()],
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

    def test_receipt_url_variant_resolves_same_pr(self) -> None:
        """A receipt probe URL with a trailing PR path still resolves the PR."""
        local_contract = _ticket_contract()
        main_contract = _ticket_contract()

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
            receipts_on_ref=[
                _receipt()
                | {
                    "probe_stdout": (
                        '{"number":949,"url":"https://github.com/OmniNode-ai/'
                        'omnibase_core/pull/949/files","state":"MERGED",'
                        '"mergeCommit":{"oid":"abcdef1234567890abcdef1234567890abcdef12"}}'
                    )
                }
            ],
        )

        result = gate.evaluate(
            ticket_id=_TICKET,
            contract=local_contract,
            receipt_dir=_RECEIPT_DIR,
            contract_rel_path=_CONTRACT_PATH,
            ticket_labels=frozenset({EnumDoneClassLabel.RUNTIME_OBSERVED.value}),
        )

        assert result.status == EnumDurableEvidenceStatus.PASS
        main_check = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN
        )
        assert main_check.passed is True

    def test_zero_receipt_bound_commits_fails_citation_check(self) -> None:
        """Tracked receipts with no PR-bound commit cannot pass vacuously."""
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
            receipts_on_ref=[],
        )

        result = gate.evaluate(
            ticket_id="OMN-9999",
            contract=empty_contract,
            receipt_dir=receipt_dir,
            contract_rel_path=default_contract_path("OMN-9999"),
        )

        assert result.status == EnumDurableEvidenceStatus.FAIL
        cite_check = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.CONTRACT_CITES_MERGE_COMMIT
        )
        assert cite_check.passed is False
        assert "No PASS receipt" in cite_check.message


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

    def test_default_invocation_passes_with_receipt_bound_merge_commit(self) -> None:
        """DEFAULT invocation PASSes when a receipt binds a merged PR commit."""
        ticket = "OMN-12574"
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
            pr_view={
                ("OmniNode-ai/onex_change_control", 2083): (
                    "MERGED",
                    "abcdef1234567890abcdef1234567890abcdef12",
                )
            },
            contract_on_main=contract,  # contract present on OCC dev
            receipts_on_ref=[
                _receipt(
                    repo="OmniNode-ai/onex_change_control",
                    pr_number=2083,
                    evidence_id="dod-boundary-clone-retry-pr",
                )
            ],
        )

        # DEFAULT invocation: caller passes ticket_id + contract + the ticket's
        # done-class label set. The gate resolves canonical paths and the dev
        # ref internally.
        result = gate.evaluate_default(
            ticket_id=ticket,
            contract=contract,
            ticket_labels=frozenset({EnumDoneClassLabel.SOURCE_DONE.value}),
        )

        assert result.status == EnumDurableEvidenceStatus.PASS
        assert all(c.passed for c in result.checks)
        assert {c.check for c in result.checks} == {
            EnumDurableEvidenceCheck.RECEIPT_TRACKED,
            EnumDurableEvidenceCheck.CONTRACT_CITES_MERGE_COMMIT,
            EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN,
            EnumDurableEvidenceCheck.DEFECT_PREVENTION_GATE,
            EnumDurableEvidenceCheck.DONE_CLASS_LABEL,
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

    def test_default_invocation_fails_without_receipt_bound_commit(self) -> None:
        """A schema-valid contract with zero bound commits must not pass."""
        ticket = "OMN-12574"
        contract: dict[str, object] = {
            "schema_version": "1.0.0",
            "ticket_id": ticket,
            "dod_evidence": [
                {
                    "id": "dod-boundary-clone-retry-pr",
                    "description": "OCC PR #2083 retries validate-boundaries clones.",
                    "checks": [{"check_type": "command", "check_value": "true"}],
                }
            ],
        }
        gate = _make_gate(
            tracked={
                (
                    _OCC_REPO,
                    DEFAULT_OCC_GOVERNANCE_REF,
                    default_receipt_dir(ticket),
                ): True,
            },
            pr_view={},
            contract_on_main=contract,
            receipts_on_ref=[],
        )

        result = gate.evaluate_default(ticket_id=ticket, contract=contract)

        assert result.status == EnumDurableEvidenceStatus.FAIL
        cite_check = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.CONTRACT_CITES_MERGE_COMMIT
        )
        assert cite_check.passed is False
        assert "No PASS receipt" in cite_check.message

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

    def test_enforce_default_threads_done_class_labels(self) -> None:
        """enforce_default() passes label context through canonical path checks."""
        ticket = "OMN-12574"
        contract: dict[str, object] = {
            "schema_version": "1.0.0",
            "ticket_id": ticket,
            "dod_evidence": [
                {
                    "id": "dod-boundary-clone-retry-pr",
                    "checks": [{"check_type": "command"}],
                }
            ],
        }
        gate = _make_gate(
            tracked={
                (
                    _OCC_REPO,
                    DEFAULT_OCC_GOVERNANCE_REF,
                    default_receipt_dir(ticket),
                ): True,
            },
            pr_view={
                ("OmniNode-ai/onex_change_control", 2083): (
                    "MERGED",
                    "abcdef1234567890abcdef1234567890abcdef12",
                )
            },
            contract_on_main=contract,
            receipts_on_ref=[
                _receipt(
                    repo="OmniNode-ai/onex_change_control",
                    pr_number=2083,
                    evidence_id="dod-boundary-clone-retry-pr",
                )
            ],
        )

        result = gate.enforce_default(
            ticket_id=ticket,
            contract=contract,
            ticket_labels=frozenset({EnumDoneClassLabel.SOURCE_DONE.value}),
        )

        done_class = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.DONE_CLASS_LABEL
        )
        assert done_class.passed is True


@pytest.mark.unit
class TestExtractReceiptMergeCommits:
    """extract_receipt_merge_commits is the parser the gate relies on."""

    def test_receipt_json_url_extracted(self) -> None:
        cites = extract_receipt_merge_commits([_receipt()])
        assert len(cites) == 1
        assert cites[0].pr_number == 949
        assert cites[0].repo == "OmniNode-ai/omnibase_core"
        assert cites[0].evidence_item_id == "dod-001"
        assert cites[0].check_type == "command"

    def test_receipt_probe_command_repo_extracted(self) -> None:
        receipt = _receipt(repo="OmniNode-ai/omnimarket", pr_number=123) | {
            "probe_stdout": "non-json output"
        }
        cites = extract_receipt_merge_commits([receipt])
        assert len(cites) == 1
        assert cites[0].pr_number == 123
        assert cites[0].repo == "OmniNode-ai/omnimarket"

    def test_dedupes_repeated_receipts(self) -> None:
        receipt = _receipt(pr_number=1, commit_sha="aaa1111")
        cites = extract_receipt_merge_commits([receipt, receipt])
        assert len(cites) == 1

    def test_skips_malformed_or_nonpass_receipts(self) -> None:
        cites = extract_receipt_merge_commits(
            [
                {"evidence_item_id": "dod-001"},
                _receipt(status="FAIL"),
                _receipt(commit_sha="abc"),
                _receipt() | {"pr_number": None},
                _receipt() | {"probe_stdout": "non-json", "probe_command": "true"},
            ]
        )
        assert cites == []

    def test_url_variants_dedupe_to_same_citation(self) -> None:
        receipt_a = _receipt(pr_number=123, commit_sha="deadbeef1234567")
        receipt_b = _receipt(pr_number=123, commit_sha="deadbeef1234567") | {
            "probe_stdout": (
                '{"number":123,"url":"https://github.com/OmniNode-ai/'
                'omnibase_core/pull/123/files","state":"MERGED",'
                '"mergeCommit":{"oid":"deadbeef1234567"}}'
            )
        }

        cites = extract_receipt_merge_commits([receipt_a, receipt_b])

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
