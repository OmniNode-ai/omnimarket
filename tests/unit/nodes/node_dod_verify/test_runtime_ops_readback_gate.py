# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-14168 acceptance regression: RUNTIME_OPS_READBACK no-PR evidence class.

Two surfaces are proven here:

* the pure :func:`evaluate_runtime_ops_readback` helper — ACCEPTS a well-formed
  readback set and REJECTS each abuse vector (self-attested, PR-bearing,
  source-diff/git-verb, no-source-change-false, empty readback, missing
  prevention follow-up, prod target, stale readback, mixed set); and
* the ``DurableEvidenceGate`` — it reads no receipt trail (OMN-20074), so a
  well-formed runtime-ops receipt set no longer passes it; the ticket is
  refused unless a product repository governs it.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
from omnimarket.nodes.node_dod_verify.services.runtime_ops_readback import (
    evaluate_runtime_ops_readback,
    is_prod_target,
    is_runtime_ops_receipt_set,
)

_TICKET = "OMN-14159"
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
    def test_well_formed_runtime_ops_set_is_refused_by_the_gate(self) -> None:
        """The set the gate used to admit from an OCC receipt trail is refused."""
        contract = _runtime_ops_contract()
        result = _gate_over_no_repo_contract().evaluate(
            ticket_id=_TICKET,
            contract=contract,
            ticket_labels=frozenset({EnumDoneClassLabel.RUNTIME_OBSERVED.value}),
        )

        assert result.status == EnumDurableEvidenceStatus.FAIL
        checks = {c.check for c in result.checks}
        assert EnumDurableEvidenceCheck.RUNTIME_OPS_READBACK not in checks
        governing = next(
            c
            for c in result.checks
            if c.check == EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN
        )
        assert governing.passed is False
        assert "no product repository governs" in governing.message
