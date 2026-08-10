# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-15817 shape 5 acceptance regression: RECEIPT_BOUND no-PR evidence class.

Two surfaces are proven here, mirroring the existing RUNTIME_OPS_READBACK
coverage (``test_runtime_ops_readback_gate.py``):

* the pure :func:`evaluate_receipt_bound` helper — ACCEPTS a well-formed
  receipt-bound set (independent verifier, non-empty observed evidence) and
  REJECTS each abuse vector (self-attested, empty readback, zero receipts);
  and
* the ``DurableEvidenceGate`` Check-2 branch — a ``proof_class:
  "receipt-bound"`` contract with a well-formed receipt set PASSES the full
  gate without ever calling the merged-PR probes, and each abuse REJECTS the
  gate. The DEFAULT (merged-PR) path is unaffected when the contract does
  not declare ``proof_class: "receipt-bound"`` — the exact OMN-15087
  motivating case (a Postgres RLS audit ticket with durable receipts and no
  product PR, permanently BLOCKED before this fix) is reproduced directly.

The merged-PR probes (``gh_pr_view``/``pr_commits``) must never be called on
the receipt-bound branch — the stubs raise if they are.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_dod_verify.models.model_durable_evidence_gate import (
    EnumDoneClassLabel,
    EnumDurableEvidenceCheck,
    EnumDurableEvidenceStatus,
)
from omnimarket.nodes.node_dod_verify.services.durable_evidence_gate import (
    DEFAULT_OCC_GOVERNANCE_REF,
    DurableEvidenceGate,
    default_contract_path,
    default_receipt_dir,
)
from omnimarket.nodes.node_dod_verify.services.receipt_bound_evidence import (
    RECEIPT_BOUND_PROOF_CLASS,
    evaluate_receipt_bound,
    is_receipt_bound_contract,
)

_OCC_REPO = "/fake/onex_change_control"
_DEV_REF = DEFAULT_OCC_GOVERNANCE_REF
_TICKET = "OMN-15087"
_RECEIPT_DIR = default_receipt_dir(_TICKET)
_CONTRACT_PATH = default_contract_path(_TICKET)
_EVIDENCE_ID = "dod-rls-role-audit"


def _receipt_bound_contract(
    *, proof_class: str | None = RECEIPT_BOUND_PROOF_CLASS
) -> dict[str, object]:
    """A minimal receipt-bound contract stub — OMN-15087's actual shape: one
    audit-query evidence item, no PR-bound checks anywhere, and a top-level
    ``proof_class`` field (omitted when ``proof_class`` is None)."""
    contract: dict[str, object] = {
        "schema_version": "1.0.0",
        "ticket_id": _TICKET,
        "dod_evidence": [
            {
                "id": _EVIDENCE_ID,
                "description": "Role/connection audit over pg_roles/pg_tables",
                "checks": [
                    {
                        "check_type": "command",
                        "check_value": (
                            'psql -c "SELECT rolsuper, rolbypassrls FROM '
                            "pg_roles WHERE rolname='app_dashboard'\""
                        ),
                    }
                ],
            }
        ],
    }
    if proof_class is not None:
        contract["proof_class"] = proof_class
    return contract


def _receipt_bound_receipt(**overrides: object) -> dict[str, object]:
    """A well-formed receipt-bound PASS receipt payload (dict form)."""
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "ticket_id": _TICKET,
        "evidence_item_id": _EVIDENCE_ID,
        "check_type": "command",
        "check_value": (
            'psql -c "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE '
            "rolname='app_dashboard'\""
        ),
        "status": "PASS",
        "run_timestamp": "2026-08-05T12:00:00Z",
        "commit_sha": "0000000",
        "runner": "impl-agent",
        "verifier": "verify-agent",
        "probe_command": (
            'psql -c "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE '
            "rolname='app_dashboard'\""
        ),
        "probe_stdout": " rolsuper | rolbypassrls \n----------+--------------\n f        | f\n",
    }
    payload.update(overrides)
    return payload


def _make_gate(
    *,
    tracked: bool,
    receipts: list[dict[str, object]],
    contract_on_main: dict[str, object] | None,
) -> DurableEvidenceGate:
    def is_receipt_tracked(repo_path: str, ref: str, receipt_dir: str) -> bool:
        return tracked

    def gh_pr_view(repo: str, pr_number: int) -> tuple[str, str | None]:
        raise AssertionError(
            "gh_pr_view must NOT be called on the receipt-bound branch"
        )

    def pr_commits(repo: str, pr_number: int) -> tuple[str, ...]:
        raise AssertionError(
            "pr_commits must NOT be called on the receipt-bound branch"
        )

    def load_contract(
        repo_path: str, ref: str, rel_path: str
    ) -> dict[str, object] | None:
        return contract_on_main

    def load_receipts(
        repo_path: str, ref: str, receipt_dir: str
    ) -> list[dict[str, object]]:
        return receipts

    return DurableEvidenceGate(
        is_receipt_tracked=is_receipt_tracked,
        gh_pr_view=gh_pr_view,
        pr_commits=pr_commits,
        load_contract_on_ref=load_contract,
        load_receipts_on_ref=load_receipts,
        occ_repo_path=_OCC_REPO,
        occ_governance_ref=_DEV_REF,
    )


def _evaluate(gate: DurableEvidenceGate, contract: dict[str, object]):
    return gate.evaluate(
        ticket_id=_TICKET,
        contract=contract,
        receipt_dir=_RECEIPT_DIR,
        contract_rel_path=_CONTRACT_PATH,
        ticket_labels=frozenset({EnumDoneClassLabel.SOURCE_DONE.value}),
    )


# ---------------------------------------------------------------------------
# Pure helper: evaluate_receipt_bound / is_receipt_bound_contract
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEvaluateReceiptBoundPure:
    def test_well_formed_set_accepts(self) -> None:
        passed, message, keys = evaluate_receipt_bound([_receipt_bound_receipt()])
        assert passed is True
        assert keys == {(_EVIDENCE_ID, "command")}
        assert "verified" in message

    def test_self_attested_rejected(self) -> None:
        passed, message, _ = evaluate_receipt_bound(
            [_receipt_bound_receipt(verifier="impl-agent")]
        )
        assert passed is False
        assert "self-attested" in message

    def test_missing_verifier_identity_rejected(self) -> None:
        passed, message, _ = evaluate_receipt_bound(
            [_receipt_bound_receipt(verifier="")]
        )
        assert passed is False
        assert "independent verifier" in message

    def test_empty_readback_rejected(self) -> None:
        passed, message, _ = evaluate_receipt_bound(
            [_receipt_bound_receipt(probe_stdout="")]
        )
        assert passed is False
        assert "probe_stdout is empty" in message

    def test_zero_receipts_rejected(self) -> None:
        passed, message, keys = evaluate_receipt_bound([])
        assert passed is False
        assert "nothing to verify" in message.lower()
        assert keys == set()

    def test_multiple_receipts_all_verified_accumulate_keys(self) -> None:
        passed, _message, keys = evaluate_receipt_bound(
            [
                _receipt_bound_receipt(),
                _receipt_bound_receipt(
                    evidence_item_id="dod-rls-role-audit-2",
                    check_type="command",
                ),
            ]
        )
        assert passed is True
        assert keys == {
            (_EVIDENCE_ID, "command"),
            ("dod-rls-role-audit-2", "command"),
        }

    def test_one_bad_receipt_fails_the_whole_set(self) -> None:
        """Fail-closed: even with one well-formed receipt in the set, a
        single self-attested receipt refuses the whole evaluation rather
        than silently dropping the bad one."""
        passed, message, keys = evaluate_receipt_bound(
            [
                _receipt_bound_receipt(),
                _receipt_bound_receipt(
                    evidence_item_id="dod-rls-role-audit-2",
                    verifier="impl-agent",
                ),
            ]
        )
        assert passed is False
        assert "self-attested" in message
        assert keys == set()

    def test_missing_evidence_item_id_rejected(self) -> None:
        """CodeRabbit finding on the introducing PR: a PASS receipt missing
        evidence_item_id must not silently contribute nothing to
        receipt_keys while still counting toward an overall PASS — Check 3
        (`receipt_keys - main_contract_keys`) is vacuously satisfied when
        receipt_keys is empty, so this would let a malformed receipt bind to
        nothing yet still pass the gate."""
        payload = _receipt_bound_receipt()
        del payload["evidence_item_id"]
        passed, message, keys = evaluate_receipt_bound([payload])
        assert passed is False
        assert "evidence_item_id" in message
        assert keys == set()

    def test_missing_check_type_rejected(self) -> None:
        payload = _receipt_bound_receipt()
        del payload["check_type"]
        passed, message, keys = evaluate_receipt_bound([payload])
        assert passed is False
        assert "check_type" in message
        assert keys == set()

    def test_blank_evidence_item_id_rejected(self) -> None:
        passed, message, keys = evaluate_receipt_bound(
            [_receipt_bound_receipt(evidence_item_id="   ")]
        )
        assert passed is False
        assert "evidence_item_id" in message
        assert keys == set()

    def test_blank_check_type_rejected(self) -> None:
        passed, message, keys = evaluate_receipt_bound(
            [_receipt_bound_receipt(check_type="")]
        )
        assert passed is False
        assert "check_type" in message
        assert keys == set()


@pytest.mark.unit
class TestIsReceiptBoundContract:
    def test_true_when_declared(self) -> None:
        assert is_receipt_bound_contract(_receipt_bound_contract()) is True

    def test_false_when_absent(self) -> None:
        assert (
            is_receipt_bound_contract(_receipt_bound_contract(proof_class=None))
            is False
        )

    @pytest.mark.parametrize("value", ["deployed", "live-readback", "code-only", ""])
    def test_false_for_other_doctrine_proof_classes(self, value: str) -> None:
        assert (
            is_receipt_bound_contract(_receipt_bound_contract(proof_class=value))
            is False
        )


# ---------------------------------------------------------------------------
# DurableEvidenceGate Check-2 branch
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDurableEvidenceGateReceiptBoundBranch:
    def test_omn_15087_motivating_case_passes_gate(self) -> None:
        """The exact OMN-15087 shape: a PR-less audit ticket with a durable,
        independently-verified receipt trail. Before this fix this ticket
        was PERMANENTLY BLOCKED at Check 2 (`No PASS receipt ... binds
        pr_number, commit_sha, and a GitHub repo`) with no route to PASS —
        this is the RED-before proof for shape 5."""
        contract = _receipt_bound_contract()
        gate = _make_gate(
            tracked=True,
            receipts=[_receipt_bound_receipt()],
            contract_on_main=contract,
        )
        result = _evaluate(gate, contract)
        assert result.status == EnumDurableEvidenceStatus.PASS, [
            (c.check, c.passed, c.message) for c in result.checks
        ]
        checks = {c.check for c in result.checks}
        assert EnumDurableEvidenceCheck.RECEIPT_BOUND in checks
        assert EnumDurableEvidenceCheck.CONTRACT_CITES_MERGE_COMMIT not in checks
        assert EnumDurableEvidenceCheck.RUNTIME_OPS_READBACK not in checks

    def test_self_attested_receipt_fails_gate(self) -> None:
        contract = _receipt_bound_contract()
        gate = _make_gate(
            tracked=True,
            receipts=[_receipt_bound_receipt(verifier="impl-agent")],
            contract_on_main=contract,
        )
        result = _evaluate(gate, contract)
        assert result.status == EnumDurableEvidenceStatus.FAIL
        rb = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.RECEIPT_BOUND
        )
        assert rb.passed is False
        assert "self-attested" in rb.message

    def test_empty_readback_fails_gate(self) -> None:
        contract = _receipt_bound_contract()
        gate = _make_gate(
            tracked=True,
            receipts=[_receipt_bound_receipt(probe_stdout="")],
            contract_on_main=contract,
        )
        result = _evaluate(gate, contract)
        assert result.status == EnumDurableEvidenceStatus.FAIL

    def test_zero_pass_receipts_fails_gate(self) -> None:
        contract = _receipt_bound_contract()
        gate = _make_gate(
            tracked=True,
            receipts=[_receipt_bound_receipt(status="FAIL")],
            contract_on_main=contract,
        )
        result = _evaluate(gate, contract)
        assert result.status == EnumDurableEvidenceStatus.FAIL

    def test_contract_on_ref_must_declare_the_receipt_bound_keys(self) -> None:
        """Check 3 (CONTRACT_ON_OCC_MAIN) still applies unchanged: the
        receipt-bound evidence item's key must appear in the governance-ref
        contract, exactly as for the merged-PR / runtime-ops branches."""
        contract = _receipt_bound_contract()
        stale_contract_on_main = _receipt_bound_contract()
        stale_contract_on_main["dod_evidence"] = []  # missing the bound key
        gate = _make_gate(
            tracked=True,
            receipts=[_receipt_bound_receipt()],
            contract_on_main=stale_contract_on_main,
        )
        result = _evaluate(gate, contract)
        assert result.status == EnumDurableEvidenceStatus.FAIL
        contract_check = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN
        )
        assert contract_check.passed is False

    def test_receipt_not_tracked_still_fails_check_1(self) -> None:
        """Check 1 (RECEIPT_TRACKED) is unaffected by the new branch — a
        receipt-bound contract with no tracked receipt still fails closed."""
        contract = _receipt_bound_contract()
        gate = _make_gate(
            tracked=False,
            receipts=[_receipt_bound_receipt()],
            contract_on_main=contract,
        )
        result = _evaluate(gate, contract)
        assert result.status == EnumDurableEvidenceStatus.FAIL
        rt = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.RECEIPT_TRACKED
        )
        assert rt.passed is False

    def test_default_merged_pr_path_unaffected_when_proof_class_absent(
        self,
    ) -> None:
        """Non-weakening regression: a contract that does NOT declare
        proof_class: "receipt-bound" must still route through the ORIGINAL
        merged-PR check — a PR-less receipt set on such a contract still
        fails exactly as it did before this fix (this is what made OMN-15087
        blocked in the first place; only a contract that opts in via
        proof_class gets the new path)."""
        contract = _receipt_bound_contract(proof_class=None)
        gate = _make_gate(
            tracked=True,
            receipts=[_receipt_bound_receipt()],  # no pr_number/commit binding
            contract_on_main=contract,
        )
        result = _evaluate(gate, contract)
        assert result.status == EnumDurableEvidenceStatus.FAIL
        checks = {c.check for c in result.checks}
        assert EnumDurableEvidenceCheck.CONTRACT_CITES_MERGE_COMMIT in checks
        assert EnumDurableEvidenceCheck.RECEIPT_BOUND not in checks
        cite_check = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.CONTRACT_CITES_MERGE_COMMIT
        )
        assert cite_check.passed is False
        assert "binds pr_number" in cite_check.message
