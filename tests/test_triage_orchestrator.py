# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Task 4: Tests for node_merge_sweep_triage_orchestrator [OMN-8959].

Tests verify the 14-row decision table via mocked subprocess calls.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from omnimarket.nodes.node_merge_sweep_compute.handlers.handler_merge_sweep import (
    EnumPRTrack,
    ModelClassifiedPR,
    ModelMergeSweepResult,
    ModelPRInfo,
)
from omnimarket.nodes.node_merge_sweep_triage_orchestrator.handlers.handler_triage import (
    HandlerTriageOrchestrator,
)
from omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request import (
    ModelAutoMergeArmCommand,
    ModelCiRerunCommand,
    ModelRebaseCommand,
    ModelTriageRequest,
)

_RUN_ID = UUID("00000000-0000-4000-a000-000000000001")
_CORR_ID = UUID("00000000-0000-4000-a000-000000000002")


def _pr(
    number: int,
    mergeable: str = "MERGEABLE",
    merge_state_status: str = "CLEAN",
    review_decision: str | None = "APPROVED",
    required_checks_pass: bool = True,
    is_draft: bool = False,
    required_approving_review_count: int | None = None,
) -> ModelPRInfo:
    return ModelPRInfo(
        number=number,
        title=f"PR {number}",
        repo="OmniNode-ai/omni_home",
        mergeable=mergeable,
        merge_state_status=merge_state_status,
        review_decision=review_decision,
        required_checks_pass=required_checks_pass,
        is_draft=is_draft,
        required_approving_review_count=required_approving_review_count,
    )


def _classified(
    pr: ModelPRInfo, track: EnumPRTrack, reason: str = "test"
) -> ModelClassifiedPR:
    return ModelClassifiedPR(pr=pr, track=track, reason=reason)


def _mock_proc(stdout: dict[str, Any], returncode: int = 0) -> MagicMock:
    """Build a mock asyncio subprocess result."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(json.dumps(stdout).encode(), b""))
    return proc


def _make_request(classified: list[ModelClassifiedPR]) -> ModelTriageRequest:
    return ModelTriageRequest(
        classification=ModelMergeSweepResult(classified=classified),
        run_id=_RUN_ID,
        correlation_id=_CORR_ID,
    )


@pytest.mark.asyncio
async def test_rule_2_clean_approved_emits_auto_merge_arm() -> None:
    """Rule 2: A_UPDATE + CLEAN + APPROVED + checks_pass → AutoMergeArm."""
    pr = _pr(100, merge_state_status="CLEAN", review_decision="APPROVED")
    classified = [_classified(pr, EnumPRTrack.A_UPDATE)]
    request = _make_request(classified)

    mock_proc = _mock_proc({"id": "PR_kwXXXXXX", "headRefName": "feat/test"})
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        handler = HandlerTriageOrchestrator()
        output = await handler.handle(request)

    assert len(output.events) == 1
    cmd = output.events[0]
    assert isinstance(cmd, ModelAutoMergeArmCommand)
    assert cmd.pr_number == 100
    assert cmd.pr_node_id == "PR_kwXXXXXX"
    assert cmd.head_ref_name == "feat/test"
    assert cmd.total_prs == 1


@pytest.mark.asyncio
async def test_rule_3_behind_approved_emits_rebase() -> None:
    """Rule 3: A_UPDATE + BEHIND + APPROVED → Rebase."""
    pr = _pr(200, merge_state_status="BEHIND", review_decision="APPROVED")
    classified = [_classified(pr, EnumPRTrack.A_UPDATE)]
    request = _make_request(classified)

    mock_proc = _mock_proc(
        {
            "headRefName": "feat/behind",
            "baseRefName": "main",
            "headRefOid": "abc123def456",
        }
    )
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        handler = HandlerTriageOrchestrator()
        output = await handler.handle(request)

    assert len(output.events) == 1
    cmd = output.events[0]
    assert isinstance(cmd, ModelRebaseCommand)
    assert cmd.pr_number == 200
    assert cmd.head_ref_name == "feat/behind"
    assert cmd.base_ref_name == "main"
    assert cmd.head_ref_oid == "abc123def456"


@pytest.mark.asyncio
async def test_rule_4_behind_not_approved_protection_requires_approval_skip() -> None:
    """Rule 4: A_UPDATE + BEHIND + not APPROVED + protection requires approval → SKIP.

    Post-OMN-9106: Rule 4 requires the approval gate to NOT be cleared, i.e. protection
    requires approval. Solo-dev repos (required=0/None) flow to Rule 3 (rebase).
    """
    pr = _pr(
        300,
        merge_state_status="BEHIND",
        review_decision=None,
        required_approving_review_count=1,
    )
    classified = [_classified(pr, EnumPRTrack.A_UPDATE)]
    request = _make_request(classified)

    handler = HandlerTriageOrchestrator()
    output = await handler.handle(request)
    assert len(output.events) == 0


@pytest.mark.asyncio
async def test_rule_1_draft_skipped() -> None:
    """Rule 1: draft PR → SKIP regardless of track."""
    pr = _pr(400, is_draft=True)
    classified = [_classified(pr, EnumPRTrack.A_UPDATE)]
    request = _make_request(classified)

    handler = HandlerTriageOrchestrator()
    output = await handler.handle(request)
    assert len(output.events) == 0


@pytest.mark.asyncio
async def test_rule_5_a_resolve_skip() -> None:
    """Rule 5: A_RESOLVE → SKIP (Phase 2 LLM)."""
    pr = _pr(500)
    classified = [_classified(pr, EnumPRTrack.A_RESOLVE)]
    request = _make_request(classified)

    handler = HandlerTriageOrchestrator()
    output = await handler.handle(request)
    assert len(output.events) == 0


@pytest.mark.asyncio
async def test_rule_6_b_polish_blocked_emits_ci_rerun() -> None:
    """Rule 6: B_POLISH + MERGEABLE + BLOCKED + checks failing → CiRerun."""
    pr = _pr(600, merge_state_status="BLOCKED", required_checks_pass=False)
    classified = [_classified(pr, EnumPRTrack.B_POLISH)]
    request = _make_request(classified)

    mock_proc = _mock_proc(
        {
            "statusCheckRollup": [
                {
                    "conclusion": "FAILURE",
                    "detailsUrl": "https://github.com/OmniNode-ai/omni_home/actions/runs/99887766",
                }
            ]
        }
    )
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        handler = HandlerTriageOrchestrator()
        output = await handler.handle(request)

    assert len(output.events) == 1
    cmd = output.events[0]
    assert isinstance(cmd, ModelCiRerunCommand)
    assert cmd.pr_number == 600
    assert cmd.run_id_github == "99887766"


@pytest.mark.asyncio
async def test_rule_13_changes_requested_skip() -> None:
    """Rule 13: CHANGES_REQUESTED → SKIP."""
    pr = _pr(700, review_decision="CHANGES_REQUESTED")
    classified = [_classified(pr, EnumPRTrack.A_UPDATE)]
    request = _make_request(classified)

    handler = HandlerTriageOrchestrator()
    output = await handler.handle(request)
    assert len(output.events) == 0


@pytest.mark.asyncio
async def test_three_pr_fixture_emits_three_typed_events() -> None:
    """3-PR fixture: 1 auto-merge-arm, 1 rebase, 1 ci-rerun."""
    pr1 = _pr(100, merge_state_status="CLEAN", review_decision="APPROVED")  # Rule 2
    pr2 = _pr(200, merge_state_status="BEHIND", review_decision="APPROVED")  # Rule 3
    pr3 = _pr(300, merge_state_status="BLOCKED", required_checks_pass=False)  # Rule 6

    classified = [
        _classified(pr1, EnumPRTrack.A_UPDATE),
        _classified(pr2, EnumPRTrack.A_UPDATE),
        _classified(pr3, EnumPRTrack.B_POLISH),
    ]
    request = _make_request(classified)

    call_count = 0

    async def fake_subprocess(*args: Any, **kwargs: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        # pr1 → GraphQL id call
        if call_count == 1:
            return _mock_proc({"id": "PR_kwAAA", "headRefName": "feat/100"})
        # pr2 → refs call
        if call_count == 2:
            return _mock_proc(
                {
                    "headRefName": "feat/200",
                    "baseRefName": "main",
                    "headRefOid": "sha200",
                }
            )
        # pr3 → statusCheckRollup call
        return _mock_proc(
            {
                "statusCheckRollup": [
                    {
                        "conclusion": "FAILURE",
                        "detailsUrl": "https://github.com/OmniNode-ai/omni_home/actions/runs/300300",
                    }
                ]
            }
        )

    with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
        handler = HandlerTriageOrchestrator()
        output = await handler.handle(request)

    assert len(output.events) == 3
    types = [type(e).__name__ for e in output.events]
    assert "ModelAutoMergeArmCommand" in types
    assert "ModelRebaseCommand" in types
    assert "ModelCiRerunCommand" in types
    # Orchestrator never returns a result payload
    assert output.result is None


# --- OMN-9106: approval-gate semantics ----------------------------------------
# merge-sweep must enqueue a CLEAN PR when branch protection does not require
# approval (required_approving_review_count in {0, None}), even if
# reviewDecision is "" / None. Reference: OMN-9106.


@pytest.mark.asyncio
async def test_omn9106_empty_review_protection_zero_enqueues() -> None:
    """reviewDecision="" + protection required=0 + CLEAN → AutoMergeArm.

    Solo-dev-configured repo (no approving review required): must enqueue.
    """
    pr = _pr(
        1344,
        merge_state_status="CLEAN",
        review_decision=None,  # inventory normalizes "" → None
        required_approving_review_count=0,
    )
    classified = [_classified(pr, EnumPRTrack.A_UPDATE)]
    request = _make_request(classified)

    mock_proc = _mock_proc({"id": "PR_kw1344", "headRefName": "feat/1344"})
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        handler = HandlerTriageOrchestrator()
        output = await handler.handle(request)

    assert len(output.events) == 1
    cmd = output.events[0]
    assert isinstance(cmd, ModelAutoMergeArmCommand)
    assert cmd.pr_number == 1344


@pytest.mark.asyncio
async def test_omn9106_empty_review_protection_none_enqueues() -> None:
    """reviewDecision=None + protection unset (None) + CLEAN → AutoMergeArm.

    Branch protection not configured at all: same semantic as required=0.
    """
    pr = _pr(
        831,
        merge_state_status="CLEAN",
        review_decision=None,
        required_approving_review_count=None,
    )
    classified = [_classified(pr, EnumPRTrack.A_UPDATE)]
    request = _make_request(classified)

    mock_proc = _mock_proc({"id": "PR_kw831", "headRefName": "feat/831"})
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        handler = HandlerTriageOrchestrator()
        output = await handler.handle(request)

    assert len(output.events) == 1
    assert isinstance(output.events[0], ModelAutoMergeArmCommand)


@pytest.mark.asyncio
async def test_omn9106_empty_review_protection_requires_approval_blocks() -> None:
    """reviewDecision=None + protection required=1 + CLEAN → SKIP.

    Protection does require approval: must NOT enqueue without an APPROVED review.
    """
    pr = _pr(
        900,
        merge_state_status="CLEAN",
        review_decision=None,
        required_approving_review_count=1,
    )
    classified = [_classified(pr, EnumPRTrack.A_UPDATE)]
    request = _make_request(classified)

    handler = HandlerTriageOrchestrator()
    output = await handler.handle(request)
    assert len(output.events) == 0


@pytest.mark.asyncio
async def test_omn9106_changes_requested_blocks_regardless_of_protection() -> None:
    """CHANGES_REQUESTED must never enqueue, even if protection does not require approval."""
    pr = _pr(
        901,
        merge_state_status="CLEAN",
        review_decision="CHANGES_REQUESTED",
        required_approving_review_count=0,
    )
    classified = [_classified(pr, EnumPRTrack.A_UPDATE)]
    request = _make_request(classified)

    handler = HandlerTriageOrchestrator()
    output = await handler.handle(request)
    assert len(output.events) == 0


@pytest.mark.asyncio
async def test_omn9106_approved_enqueues_regardless_of_protection() -> None:
    """reviewDecision=APPROVED + protection required=1 + CLEAN → AutoMergeArm."""
    pr = _pr(
        902,
        merge_state_status="CLEAN",
        review_decision="APPROVED",
        required_approving_review_count=1,
    )
    classified = [_classified(pr, EnumPRTrack.A_UPDATE)]
    request = _make_request(classified)

    mock_proc = _mock_proc({"id": "PR_kw902", "headRefName": "feat/902"})
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        handler = HandlerTriageOrchestrator()
        output = await handler.handle(request)

    assert len(output.events) == 1
    assert isinstance(output.events[0], ModelAutoMergeArmCommand)


@pytest.mark.asyncio
async def test_node_id_resolution_failure_skips_pr() -> None:
    """If GraphQL node ID resolution fails, PR is skipped (no command emitted)."""
    pr = _pr(800, merge_state_status="CLEAN", review_decision="APPROVED")
    classified = [_classified(pr, EnumPRTrack.A_UPDATE)]
    request = _make_request(classified)

    mock_proc = _mock_proc({}, returncode=1)
    mock_proc.communicate = AsyncMock(return_value=(b"", b"auth error"))
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        handler = HandlerTriageOrchestrator()
        output = await handler.handle(request)

    assert len(output.events) == 0
