# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-15817 shape 5 acceptance regression: RECEIPT_BOUND no-PR evidence class.

Two surfaces are proven here, mirroring the existing RUNTIME_OPS_READBACK
coverage (``test_runtime_ops_readback_gate.py``):

* the pure :func:`evaluate_receipt_bound` helper — ACCEPTS a well-formed
  receipt-bound set (independent verifier, non-empty observed evidence) and
  REJECTS each abuse vector (self-attested, empty readback, zero receipts);
  and
* the ``DurableEvidenceGate`` — it reads no receipt trail (OMN-20074), so the
  exact OMN-15087 shape (a Postgres RLS audit ticket with durable receipts and
  no product PR) is refused unless a product repository governs the ticket.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_dod_verify.models.model_durable_evidence_gate import (
    EnumDoneClassLabel,
    EnumDurableEvidenceCheck,
    EnumDurableEvidenceStatus,
    EnumRepoContractReadStatus,
    ModelRepoContractRead,
)
from omnimarket.nodes.node_dod_verify.services.durable_evidence_gate import (
    DurableEvidenceGate,
)
from omnimarket.nodes.node_dod_verify.services.receipt_bound_evidence import (
    RECEIPT_BOUND_PROOF_CLASS,
    evaluate_receipt_bound,
    is_receipt_bound_contract,
)

_TICKET = "OMN-15087"
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


def _gate_over_no_repo_contract() -> DurableEvidenceGate:
    """A gate whose merged PR carries no product-repository contract."""
    return DurableEvidenceGate(
        release_tags_containing=lambda _repo, _sha: ("v9.9.9",),
        index_release_files=lambda _d, _v: frozenset({"bdist_wheel", "sdist"}),
        read_repo_contract=lambda _repo, _ref, _ticket: ModelRepoContractRead(
            status=EnumRepoContractReadStatus.ABSENT
        ),
        read_repo_check_runs=lambda _repo, _sha: (),
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
# DurableEvidenceGate
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDurableEvidenceGateReceiptBoundBranch:
    def test_omn_15087_motivating_case_is_refused_by_the_gate(self) -> None:
        """The PR-less audit shape the gate used to admit from an OCC receipt trail.

        The gate reads no receipt trail (OMN-20074), so a receipt-bound ticket
        no product repository governs is refused.
        """
        contract = _receipt_bound_contract()
        result = _gate_over_no_repo_contract().evaluate(
            ticket_id=_TICKET,
            contract=contract,
            ticket_labels=frozenset({EnumDoneClassLabel.SOURCE_DONE.value}),
        )

        assert result.status == EnumDurableEvidenceStatus.FAIL
        checks = {c.check for c in result.checks}
        assert EnumDurableEvidenceCheck.RECEIPT_BOUND not in checks
        governing = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN
        )
        assert governing.passed is False
        assert "no product repository governs" in governing.message
