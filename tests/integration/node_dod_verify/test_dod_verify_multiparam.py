# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration proof for node_dod_verify (OMN-13679, WS-5).

Two surfaces, both Variant A (COMPUTE, direct in-process handler call):

1. ``HandlerDodVerify.handle`` with pre-collected ``evidence_results`` — the
   pure aggregation path. Parametrized over evidence mixes; asserts the typed
   ``ModelDodVerifyState`` roll-up (status + verified/failed/skipped counts).

2. ``DurableEvidenceGate`` — the pre-Linear-Done gate. Parametrized over
   deterministic injected product-repository readers (no git/gh/subprocess).
   Covers a full PASS and the refusals: no product-repository contract, a red
   ``repo-evidence / dod-verify`` run, and no merged PR (no other source
   decides a ticket the product repository does not govern).

The I/O boundary is satisfied by constructor-injected collaborators
(``evidence_results`` for the handler; the Protocol readers for the gate).
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
    EnumRepoContractReadStatus,
    ModelRepoContractRead,
    ModelRepoEvidenceCheckRun,
    ModelTicketMergedPr,
)
from omnimarket.nodes.node_dod_verify.services.durable_evidence_gate import (
    REPO_EVIDENCE_APP_SLUG,
    REPO_EVIDENCE_CHECK_NAME,
    DurableEvidenceGate,
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
# Surface 2 — DurableEvidenceGate (product-repository contract and check run)
# ---------------------------------------------------------------------------

_TICKET = "OMN-13679"
_REPO = "OmniNode-ai/omnimarket"
_HEAD_SHA = "a" * 40
_MERGE_SHA = "b" * 40
_DESCRIPTION = (
    "## Acceptance criteria\n\n"
    "- [ ] AC1: it works -- falsifier: uv run pytest tests/a.py\n"
)


def _contract() -> dict[str, object]:
    return {
        "schema_version": "1.0.0",
        "ticket_id": _TICKET,
        "dod_evidence": [
            {
                "id": "dod-001",
                "description": "Code change shipped",
                "checks": [
                    {"check_type": "test_passes", "check_value": "uv run pytest a.py"}
                ],
                "binds_ac": ["AC1"],
            }
        ],
    }


def _merged_pr() -> ModelTicketMergedPr:
    return ModelTicketMergedPr(
        repo=_REPO,
        pr_number=1471,
        head_sha=_HEAD_SHA,
        merge_commit_sha=_MERGE_SHA,
        merged_at="2026-10-09T12:00:00Z",
    )


def _make_gate(
    *, contract_in_repo: dict[str, object] | None, conclusion: str
) -> DurableEvidenceGate:
    def read_repo_contract(
        repo: str, ref: str, ticket_id: str
    ) -> ModelRepoContractRead:
        if contract_in_repo is None:
            return ModelRepoContractRead(status=EnumRepoContractReadStatus.ABSENT)
        return ModelRepoContractRead(
            status=EnumRepoContractReadStatus.FOUND, contract=contract_in_repo
        )

    def read_repo_check_runs(
        repo: str, sha: str
    ) -> tuple[ModelRepoEvidenceCheckRun, ...] | None:
        return (
            ModelRepoEvidenceCheckRun(
                id=7001,
                name=REPO_EVIDENCE_CHECK_NAME,
                app_slug=REPO_EVIDENCE_APP_SLUG,
                status="completed",
                conclusion=conclusion,
            ),
        )

    return DurableEvidenceGate(
        release_tags_containing=lambda _repo, _sha: ("v9.9.9",),
        index_release_files=lambda _d, _v: frozenset({"bdist_wheel", "sdist"}),
        read_repo_contract=read_repo_contract,
        read_repo_check_runs=read_repo_check_runs,
    )


# Each case: (gate kwargs, merged PRs, ticket_labels, expected overall status,
# failing check or None when the whole gate passes).
_GATE_CASES = [
    pytest.param(
        {"contract_in_repo": _contract(), "conclusion": "success"},
        (_merged_pr(),),
        frozenset({"source-done"}),
        EnumDurableEvidenceStatus.PASS,
        None,
        id="full-pass-repo-contract-and-green-run",
    ),
    pytest.param(
        {"contract_in_repo": None, "conclusion": "success"},
        (_merged_pr(),),
        frozenset({"source-done"}),
        EnumDurableEvidenceStatus.FAIL,
        EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN,
        id="negative-no-product-repository-contract",
    ),
    pytest.param(
        {"contract_in_repo": _contract(), "conclusion": "failure"},
        (_merged_pr(),),
        frozenset({"source-done"}),
        EnumDurableEvidenceStatus.FAIL,
        EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN,
        id="negative-red-repo-evidence-run",
    ),
    pytest.param(
        {"contract_in_repo": _contract(), "conclusion": "success"},
        (),
        frozenset({"source-done"}),
        EnumDurableEvidenceStatus.FAIL,
        EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN,
        id="negative-no-merged-pr",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("gate_kwargs", "merged_prs", "labels", "expected_status", "failing_check"),
    _GATE_CASES,
)
def test_durable_evidence_gate_multiparam(
    gate_kwargs: dict[str, object],
    merged_prs: tuple[ModelTicketMergedPr, ...],
    labels: frozenset[str],
    expected_status: EnumDurableEvidenceStatus,
    failing_check: EnumDurableEvidenceCheck | None,
) -> None:
    gate = _make_gate(**gate_kwargs)  # type: ignore[arg-type]
    result = gate.evaluate(
        ticket_id=_TICKET,
        contract=_contract(),
        ticket_labels=labels,
        merged_prs=merged_prs,
        ticket_description=_DESCRIPTION,
    )

    assert result.status == expected_status
    by_check = {c.check: c for c in result.checks}

    if failing_check is None:
        # Full PASS: every check passed.
        assert all(c.passed for c in result.checks), [
            (c.check.value, c.message) for c in result.checks if not c.passed
        ]
        assert by_check[EnumDurableEvidenceCheck.CONTRACT_ON_OCC_MAIN].passed is True
    else:
        # NEGATIVE CONTROL: the named check must be the failing surface.
        assert by_check[failing_check].passed is False
        assert by_check[failing_check].message  # carries a remediation hint
