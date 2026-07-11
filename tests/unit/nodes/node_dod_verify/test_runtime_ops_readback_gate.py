# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-14168 acceptance regression: RUNTIME_OPS_READBACK no-PR evidence class.

Two surfaces are proven here:

* the pure :func:`evaluate_runtime_ops_readback` helper — ACCEPTS a well-formed
  readback set and REJECTS each abuse vector (self-attested, PR-bearing,
  source-diff/git-verb, no-source-change-false, empty readback, missing
  prevention follow-up, prod target, stale readback, mixed set); and
* the ``DurableEvidenceGate`` Check-2 branch — a no-PR runtime-ops receipt set
  PASSES the full gate, and each abuse REJECTS the gate. Check 1
  (RECEIPT_TRACKED) and Check 5 (DONE_CLASS_LABEL) still apply unchanged.

The merged-PR probe (``gh_pr_view``) must never be called on the runtime-ops
branch — the stub raises if it is.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
from omnimarket.nodes.node_dod_verify.services.runtime_ops_readback import (
    evaluate_runtime_ops_readback,
    is_prod_target,
    is_runtime_ops_receipt_set,
)

_OCC_REPO = "/fake/onex_change_control"
_DEV_REF = DEFAULT_OCC_GOVERNANCE_REF
_TICKET = "OMN-14159"
_RECEIPT_DIR = default_receipt_dir(_TICKET)
_CONTRACT_PATH = default_contract_path(_TICKET)
_EVIDENCE_ID = "dod-runtime"

_VERBS = frozenset(
    {"patch", "rollout", "scale", "restart", "recreate", "config-repair"}
)


def _runtime_ops_contract() -> dict[str, object]:
    """A minimal runtime-ops contract stub with one runtime_readback item."""
    return {
        "schema_version": "1.0.0",
        "ticket_id": _TICKET,
        "dod_evidence": [
            {
                "id": _EVIDENCE_ID,
                "description": "no-source-change runtime-ops fix, verified by readback",
                "checks": [{"check_type": "runtime_readback", "check_value": "live"}],
            }
        ],
    }


def _runtime_ops_receipt(**overrides: object) -> dict[str, object]:
    """A well-formed RUNTIME_OPS readback receipt payload (dict form)."""
    payload: dict[str, object] = {
        "schema_version": "1.0.0",
        "ticket_id": _TICKET,
        "evidence_item_id": _EVIDENCE_ID,
        "check_type": "runtime_readback",
        "check_value": "kubectl get pods -n onex-dev",
        "status": "PASS",
        "run_timestamp": "2026-07-08T22:11:00Z",
        "commit_sha": "0000000",
        "runner": "impl-agent",
        "verifier": "verify-agent",
        "probe_command": "kubectl -n onex-dev get pods",
        "probe_stdout": "omnidash-76797d58ff-dmjgv 1/1 Running 0 117m\n",
        "evidence_class": "runtime_ops",
        "mutation_command": (
            "kubectl -n onex-dev patch deployment omnidash --type strategic ..."
        ),
        "mutation_verb": "patch",
        "target_identity": "onex-dev/Deployment/omnidash",
        "no_source_change": True,
        "prevention_followup": "OMN-14161",
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
            "gh_pr_view must NOT be called on the runtime-ops readback branch"
        )

    def pr_commits(repo: str, pr_number: int) -> tuple[str, ...]:
        raise AssertionError(
            "pr_commits must NOT be called on the runtime-ops readback branch"
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
        ticket_labels=frozenset({EnumDoneClassLabel.RUNTIME_OBSERVED.value}),
    )


# ---------------------------------------------------------------------------
# Pure helper: evaluate_runtime_ops_readback
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEvaluateRuntimeOpsReadbackPure:
    def test_well_formed_set_accepts(self) -> None:
        passed, message, keys = evaluate_runtime_ops_readback(
            [_runtime_ops_receipt()], verb_allowlist=_VERBS
        )
        assert passed is True
        assert keys == {(_EVIDENCE_ID, "runtime_readback")}
        assert "verified" in message

    def test_self_attested_rejected(self) -> None:
        passed, message, _ = evaluate_runtime_ops_readback(
            [_runtime_ops_receipt(verifier="impl-agent")], verb_allowlist=_VERBS
        )
        assert passed is False
        assert "self-attested" in message

    def test_pr_bearing_rejected(self) -> None:
        passed, message, _ = evaluate_runtime_ops_readback(
            [_runtime_ops_receipt(pr_number=1349)], verb_allowlist=_VERBS
        )
        assert passed is False
        assert "pr_number/pr_url" in message

    def test_git_verb_rejected(self) -> None:
        passed, message, _ = evaluate_runtime_ops_readback(
            [_runtime_ops_receipt(mutation_verb="git")], verb_allowlist=_VERBS
        )
        assert passed is False
        assert "not in the governed" in message

    def test_no_source_change_false_rejected(self) -> None:
        passed, message, _ = evaluate_runtime_ops_readback(
            [_runtime_ops_receipt(no_source_change=False)], verb_allowlist=_VERBS
        )
        assert passed is False
        assert "no_source_change" in message

    def test_empty_readback_rejected(self) -> None:
        passed, message, _ = evaluate_runtime_ops_readback(
            [_runtime_ops_receipt(probe_stdout="")], verb_allowlist=_VERBS
        )
        assert passed is False
        assert "probe_stdout is empty" in message

    def test_missing_prevention_followup_rejected(self) -> None:
        passed, message, _ = evaluate_runtime_ops_readback(
            [_runtime_ops_receipt(prevention_followup="")], verb_allowlist=_VERBS
        )
        assert passed is False
        assert "prevention_followup" in message

    def test_prod_target_rejected(self) -> None:
        passed, message, _ = evaluate_runtime_ops_readback(
            [_runtime_ops_receipt(target_identity="onex-prod/Deployment/omnidash")],
            verb_allowlist=_VERBS,
        )
        assert passed is False
        assert "prod" in message.lower()
        assert "OMN-13418" in message

    def test_stale_readback_rejected(self) -> None:
        now = datetime(2026, 7, 9, 0, 0, 0, tzinfo=UTC)
        # run_timestamp is 2026-07-08T22:11Z → ~1h49m old; window = 30 min.
        passed, message, _ = evaluate_runtime_ops_readback(
            [_runtime_ops_receipt()],
            verb_allowlist=_VERBS,
            now=now,
            max_age_seconds=1800,
        )
        assert passed is False
        assert "stale" in message

    def test_fresh_readback_within_window_accepts(self) -> None:
        now = datetime(2026, 7, 8, 22, 20, 0, tzinfo=UTC)  # 9 min after readback
        passed, _message, _ = evaluate_runtime_ops_readback(
            [_runtime_ops_receipt()],
            verb_allowlist=_VERBS,
            now=now,
            max_age_seconds=1800,
        )
        assert passed is True

    def test_mixed_set_rejected(self) -> None:
        pr_receipt = _runtime_ops_receipt(
            evidence_class="backend",
            evidence_item_id="dod-pr",
            pr_number=1349,
            no_source_change=False,
            mutation_verb=None,
        )
        passed, message, _ = evaluate_runtime_ops_readback(
            [_runtime_ops_receipt(), pr_receipt], verb_allowlist=_VERBS
        )
        assert passed is False
        assert "mixes" in message.lower() or "mis-declared" in message.lower()


@pytest.mark.unit
class TestRuntimeOpsHelperPredicates:
    def test_is_runtime_ops_receipt_set_true(self) -> None:
        assert is_runtime_ops_receipt_set([_runtime_ops_receipt()]) is True

    def test_is_runtime_ops_receipt_set_false_for_pr_receipt(self) -> None:
        assert (
            is_runtime_ops_receipt_set(
                [_runtime_ops_receipt(evidence_class="backend", status="PASS")]
            )
            is False
        )

    def test_is_runtime_ops_receipt_set_ignores_non_pass(self) -> None:
        assert (
            is_runtime_ops_receipt_set([_runtime_ops_receipt(status="ADVISORY")])
            is False
        )

    @pytest.mark.parametrize(
        ("target", "expected"),
        [
            ("onex-prod/Deployment/x", True),
            ("omnibase-infra-prod", True),
            ("prod", True),
            ("onex-dev/Deployment/omnidash", False),
            ("product-service", False),
            ("stability-test", False),
        ],
    )
    def test_is_prod_target(self, target: str, expected: bool) -> None:
        assert is_prod_target(target) is expected


# ---------------------------------------------------------------------------
# DurableEvidenceGate Check-2 branch
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDurableEvidenceGateRuntimeOpsBranch:
    def test_well_formed_runtime_ops_set_passes_gate(self) -> None:
        contract = _runtime_ops_contract()
        gate = _make_gate(
            tracked=True,
            receipts=[_runtime_ops_receipt()],
            contract_on_main=contract,
        )
        result = _evaluate(gate, contract)
        assert result.status == EnumDurableEvidenceStatus.PASS
        assert all(c.passed for c in result.checks)
        checks = {c.check for c in result.checks}
        assert EnumDurableEvidenceCheck.RUNTIME_OPS_READBACK in checks
        assert EnumDurableEvidenceCheck.CONTRACT_CITES_MERGE_COMMIT not in checks

    def test_self_attested_receipt_fails_gate(self) -> None:
        contract = _runtime_ops_contract()
        gate = _make_gate(
            tracked=True,
            receipts=[_runtime_ops_receipt(verifier="impl-agent")],
            contract_on_main=contract,
        )
        result = _evaluate(gate, contract)
        assert result.status == EnumDurableEvidenceStatus.FAIL
        ro = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.RUNTIME_OPS_READBACK
        )
        assert ro.passed is False
        assert "self-attested" in ro.message

    def test_pr_bearing_receipt_fails_gate(self) -> None:
        contract = _runtime_ops_contract()
        gate = _make_gate(
            tracked=True,
            receipts=[_runtime_ops_receipt(pr_number=1349)],
            contract_on_main=contract,
        )
        result = _evaluate(gate, contract)
        assert result.status == EnumDurableEvidenceStatus.FAIL

    def test_git_verb_receipt_fails_gate(self) -> None:
        contract = _runtime_ops_contract()
        gate = _make_gate(
            tracked=True,
            receipts=[_runtime_ops_receipt(mutation_verb="git")],
            contract_on_main=contract,
        )
        result = _evaluate(gate, contract)
        assert result.status == EnumDurableEvidenceStatus.FAIL

    def test_untracked_receipt_fails_gate(self) -> None:
        contract = _runtime_ops_contract()
        gate = _make_gate(
            tracked=False,
            receipts=[_runtime_ops_receipt()],
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

    def test_prod_target_fails_gate(self) -> None:
        contract = _runtime_ops_contract()
        gate = _make_gate(
            tracked=True,
            receipts=[
                _runtime_ops_receipt(target_identity="onex-prod/Deployment/omnidash")
            ],
            contract_on_main=contract,
        )
        result = _evaluate(gate, contract)
        assert result.status == EnumDurableEvidenceStatus.FAIL

    def test_missing_prevention_followup_fails_gate(self) -> None:
        contract = _runtime_ops_contract()
        gate = _make_gate(
            tracked=True,
            receipts=[_runtime_ops_receipt(prevention_followup="")],
            contract_on_main=contract,
        )
        result = _evaluate(gate, contract)
        assert result.status == EnumDurableEvidenceStatus.FAIL

    def test_stale_contract_on_ref_fails_gate(self) -> None:
        # Receipt is well-formed, but the runtime-ops contract stub is absent on
        # the governance ref (Check 3): the readback check passes, Check 3 fails.
        contract = _runtime_ops_contract()
        gate = _make_gate(
            tracked=True,
            receipts=[_runtime_ops_receipt()],
            contract_on_main=None,
        )
        result = _evaluate(gate, contract)
        assert result.status == EnumDurableEvidenceStatus.FAIL
        c3 = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN
        )
        assert c3.passed is False

    def test_no_done_class_label_fails_gate(self) -> None:
        contract = _runtime_ops_contract()
        gate = _make_gate(
            tracked=True,
            receipts=[_runtime_ops_receipt()],
            contract_on_main=contract,
        )
        result = gate.evaluate(
            ticket_id=_TICKET,
            contract=contract,
            receipt_dir=_RECEIPT_DIR,
            contract_rel_path=_CONTRACT_PATH,
            ticket_labels=frozenset(),  # no done-class label
        )
        assert result.status == EnumDurableEvidenceStatus.FAIL
        label = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.DONE_CLASS_LABEL
        )
        assert label.passed is False

    def test_freshness_not_checked_on_gate_surface_a(self) -> None:
        # The gate itself does not inject a clock — a receipt that is "old" by
        # wall-clock still passes Surface A (staleness is a Surface-B live-reread
        # concern). This documents the deliberate boundary.
        old_ts = (datetime.now(tz=UTC) - timedelta(days=365)).isoformat()
        contract = _runtime_ops_contract()
        gate = _make_gate(
            tracked=True,
            receipts=[_runtime_ops_receipt(run_timestamp=old_ts)],
            contract_on_main=contract,
        )
        result = _evaluate(gate, contract)
        assert result.status == EnumDurableEvidenceStatus.PASS
