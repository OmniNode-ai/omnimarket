# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration proof for node_dod_verify (OMN-13679, WS-5).

Two surfaces, both Variant A (COMPUTE, direct in-process handler call):

1. ``HandlerDodVerify.handle`` with pre-collected ``evidence_results`` — the
   pure aggregation path. Parametrized over evidence mixes; asserts the typed
   ``ModelDodVerifyState`` roll-up (status + verified/failed/skipped counts).

2. ``DurableEvidenceGate`` — the pre-Linear-Done gate. Parametrized over
   deterministic injected probe stubs (no git/gh/subprocess). Covers the
   WAVE CAVEAT cases explicitly: a full PASS, a RECEIPT_TRACKED failure, and
   a CONTRACT_CITES_MERGE_COMMIT failure (PR not merged / zero citations).

The I/O boundary is satisfied by constructor-injected collaborators
(``evidence_results`` for the handler; the four Protocol probes for the gate).
Nothing is monkeypatched.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from omnimarket.nodes.node_dod_verify.handlers.handler_dod_verify import (
    HandlerDodVerify,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_start_command import (
    ModelDodVerifyStartCommand,
)
from omnimarket.nodes.node_dod_verify.models.model_dod_verify_state import (
    EnumDodVerifyStatus,
    EnumEvidenceCheckStatus,
    ModelDodVerifyState,
    ModelEvidenceCheckResult,
)
from omnimarket.nodes.node_dod_verify.models.model_durable_evidence_gate import (
    EnumDurableEvidenceCheck,
    EnumDurableEvidenceStatus,
)
from omnimarket.nodes.node_dod_verify.services.durable_evidence_gate import (
    DEFAULT_OCC_GOVERNANCE_REF,
    DurableEvidenceGate,
    default_contract_path,
    default_receipt_dir,
)

# ---------------------------------------------------------------------------
# Surface 1 — HandlerDodVerify evidence roll-up
# ---------------------------------------------------------------------------


def _check(
    status: EnumEvidenceCheckStatus, evidence_id: str = "dod-001"
) -> ModelEvidenceCheckResult:
    return ModelEvidenceCheckResult(
        evidence_id=evidence_id,
        description="synthetic check",
        status=status,
    )


_V = EnumEvidenceCheckStatus.VERIFIED
_F = EnumEvidenceCheckStatus.FAILED
_S = EnumEvidenceCheckStatus.SKIPPED

_HANDLER_CASES = [
    pytest.param(
        [_check(_V), _check(_V, "dod-002")],
        {
            "status": EnumDodVerifyStatus.VERIFIED,
            "total": 2,
            "verified": 2,
            "failed": 0,
            "skipped": 0,
        },
        id="all-verified",
    ),
    pytest.param(
        [_check(_V), _check(_F, "dod-002")],
        {
            "status": EnumDodVerifyStatus.FAILED,
            "total": 2,
            "verified": 1,
            "failed": 1,
            "skipped": 0,
        },
        id="negative-one-failed",
    ),
    pytest.param(
        [_check(_S), _check(_S, "dod-002")],
        {
            "status": EnumDodVerifyStatus.SKIPPED,
            "total": 2,
            "verified": 0,
            "failed": 0,
            "skipped": 2,
        },
        id="all-skipped",
    ),
    pytest.param(
        [],
        {
            "status": EnumDodVerifyStatus.SKIPPED,
            "total": 0,
            "verified": 0,
            "failed": 0,
            "skipped": 0,
        },
        id="empty-evidence",
    ),
    pytest.param(
        [_check(_V), _check(_S, "dod-002")],
        {
            "status": EnumDodVerifyStatus.VERIFIED,
            "total": 2,
            "verified": 1,
            "failed": 0,
            "skipped": 1,
        },
        id="mixed-verified-skipped",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(("evidence", "expected"), _HANDLER_CASES)
def test_dod_verify_handler_multiparam(
    evidence: list[ModelEvidenceCheckResult], expected: dict[str, object]
) -> None:
    command = ModelDodVerifyStartCommand(ticket_id="OMN-13679", correlation_id=uuid4())
    state = HandlerDodVerify()._handle_typed(command, evidence)

    assert isinstance(state, ModelDodVerifyState)
    assert state.status == expected["status"]
    assert state.total_checks == expected["total"]
    assert state.verified_count == expected["verified"]
    assert state.failed_count == expected["failed"]
    assert state.skipped_count == expected["skipped"]
    assert state.ticket_id == "OMN-13679"
    assert state.correlation_id == command.correlation_id


# ---------------------------------------------------------------------------
# Surface 2 — DurableEvidenceGate (RECEIPT_TRACKED, CONTRACT_CITES_MERGE_COMMIT)
# ---------------------------------------------------------------------------

_OCC_REPO = "/fake/onex_change_control"
_DEV_REF = DEFAULT_OCC_GOVERNANCE_REF
_TICKET = "OMN-13679"
_RECEIPT_DIR = default_receipt_dir(_TICKET)
_CONTRACT_PATH = default_contract_path(_TICKET)
_MERGE_SHA = "abcdef1234567890abcdef1234567890abcdef12"


def _contract() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "ticket_id": _TICKET,
        "dod_evidence": [
            {
                "id": "dod-001",
                "description": "Code change shipped",
                "checks": [{"check_type": "command", "check_value": "true"}],
            }
        ],
    }


def _receipt(
    *, pr_number: int = 1471, commit_sha: str = _MERGE_SHA, status: str = "PASS"
) -> dict[str, object]:
    repo = "OmniNode-ai/omnimarket"
    return {
        "schema_version": "1.0.0",
        "ticket_id": _TICKET,
        "evidence_item_id": "dod-001",
        "check_type": "command",
        "check_value": "true",
        "status": status,
        "commit_sha": commit_sha,
        "pr_number": pr_number,
        "probe_stdout": (
            f'{{"number":{pr_number},"url":"https://github.com/{repo}/pull/'
            f'{pr_number}","state":"MERGED","mergeCommit":{{"oid":"{commit_sha}"}}}}'
        ),
    }


def _make_gate(
    *,
    receipt_tracked: bool,
    pr_view: dict[tuple[str, int], tuple[str, str | None]],
    receipts: list[dict[str, object]],
    contract_on_ref: dict[str, object] | None,
) -> DurableEvidenceGate:
    def is_receipt_tracked(repo_path: str, ref: str, receipt_dir: str) -> bool:
        return receipt_tracked

    def gh_pr_view(repo: str, pr_number: int) -> tuple[str, str | None]:
        return pr_view[(repo, pr_number)]

    def load_contract(
        repo_path: str, ref: str, rel_path: str
    ) -> dict[str, object] | None:
        return contract_on_ref

    def load_receipts(
        repo_path: str, ref: str, receipt_dir: str
    ) -> list[dict[str, object]]:
        return receipts

    return DurableEvidenceGate(
        is_receipt_tracked=is_receipt_tracked,
        gh_pr_view=gh_pr_view,
        load_contract_on_ref=load_contract,
        load_receipts_on_ref=load_receipts,
        occ_repo_path=_OCC_REPO,
    )


# Each case: (gate kwargs, ticket_labels, expected overall status, failing check
# or None when the whole gate passes).
_GATE_CASES = [
    pytest.param(
        {
            "receipt_tracked": True,
            "pr_view": {("OmniNode-ai/omnimarket", 1471): ("MERGED", _MERGE_SHA)},
            "receipts": [_receipt()],
            "contract_on_ref": _contract(),
        },
        frozenset({"source-done"}),
        EnumDurableEvidenceStatus.PASS,
        None,
        id="full-pass-receipt-tracked-and-merge-cited",
    ),
    pytest.param(
        {
            "receipt_tracked": False,
            "pr_view": {("OmniNode-ai/omnimarket", 1471): ("MERGED", _MERGE_SHA)},
            "receipts": [_receipt()],
            "contract_on_ref": _contract(),
        },
        frozenset({"source-done"}),
        EnumDurableEvidenceStatus.FAIL,
        EnumDurableEvidenceCheck.RECEIPT_TRACKED,
        id="negative-receipt-not-tracked",
    ),
    pytest.param(
        {
            "receipt_tracked": True,
            "pr_view": {("OmniNode-ai/omnimarket", 1471): ("CLOSED", None)},
            "receipts": [_receipt()],
            "contract_on_ref": _contract(),
        },
        frozenset({"source-done"}),
        EnumDurableEvidenceStatus.FAIL,
        EnumDurableEvidenceCheck.CONTRACT_CITES_MERGE_COMMIT,
        id="negative-cites-non-merged-pr",
    ),
    pytest.param(
        {
            "receipt_tracked": True,
            "pr_view": {},
            "receipts": [],
            "contract_on_ref": _contract(),
        },
        frozenset({"source-done"}),
        EnumDurableEvidenceStatus.FAIL,
        EnumDurableEvidenceCheck.CONTRACT_CITES_MERGE_COMMIT,
        id="negative-zero-receipt-citations",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("gate_kwargs", "labels", "expected_status", "failing_check"), _GATE_CASES
)
def test_durable_evidence_gate_multiparam(
    gate_kwargs: dict[str, object],
    labels: frozenset[str],
    expected_status: EnumDurableEvidenceStatus,
    failing_check: EnumDurableEvidenceCheck | None,
) -> None:
    gate = _make_gate(**gate_kwargs)  # type: ignore[arg-type]
    result = gate.evaluate_default(
        ticket_id=_TICKET,
        contract=_contract(),
        ticket_labels=labels,
    )

    assert result.status == expected_status
    by_check = {c.check: c for c in result.checks}

    if failing_check is None:
        # Full PASS: every check passed.
        assert all(c.passed for c in result.checks), [
            (c.check.value, c.message) for c in result.checks if not c.passed
        ]
        assert by_check[EnumDurableEvidenceCheck.RECEIPT_TRACKED].passed is True
        assert (
            by_check[EnumDurableEvidenceCheck.CONTRACT_CITES_MERGE_COMMIT].passed
            is True
        )
    else:
        # NEGATIVE CONTROL: the named check must be the failing surface.
        assert by_check[failing_check].passed is False
        assert by_check[failing_check].message  # carries a remediation hint
