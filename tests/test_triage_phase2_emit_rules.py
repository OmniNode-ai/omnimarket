# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for handler_triage Phase 2 emit rules [OMN-8988].

Covers:
- Rule 5 (A_RESOLVE)         → ModelThreadReplyCommand
- Rule 7 (B_POLISH DIRTY)    → ModelConflictHunkCommand
- Rule 9 (B_POLISH DIRTY)    → ModelCiFixCommand
- Skip variants when resolution helpers return empty/None
- Existing Phase 1 rules not regressed by Phase 2 additions
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from omnimarket.nodes.node_merge_sweep.handlers.handler_merge_sweep import (
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
    ModelCiFixCommand,
    ModelCiRerunCommand,
    ModelConflictHunkCommand,
    ModelThreadReplyCommand,
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
    )


def _classified(
    pr: ModelPRInfo, track: EnumPRTrack, reason: str = "test"
) -> ModelClassifiedPR:
    return ModelClassifiedPR(pr=pr, track=track, reason=reason)


def _mock_proc(stdout: dict[str, Any], returncode: int = 0) -> MagicMock:
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


# ---------------------------------------------------------------------------
# Rule 5: A_RESOLVE → ModelThreadReplyCommand
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rule_5_a_resolve_emits_thread_reply() -> None:
    """Rule 5: A_RESOLVE with open threads → ModelThreadReplyCommand."""
    pr = _pr(500)
    classified = [_classified(pr, EnumPRTrack.A_RESOLVE)]
    request = _make_request(classified)

    thread_data = {
        "reviewThreads": [
            {"isResolved": False, "comments": [{"id": "IC_abc123"}]},
            {"isResolved": False, "comments": [{"id": "IC_def456"}]},
        ]
    }
    mock_proc = _mock_proc(thread_data)
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        handler = HandlerTriageOrchestrator()
        output = await handler.handle(request)

    assert len(output.events) == 1
    cmd = output.events[0]
    assert isinstance(cmd, ModelThreadReplyCommand)
    assert cmd.pr_number == 500
    assert cmd.repo == "OmniNode-ai/omni_home"
    assert "IC_abc123" in cmd.thread_comment_ids
    assert "IC_def456" in cmd.thread_comment_ids
    assert cmd.routing_policy is not None
    assert cmd.run_id == str(_RUN_ID)
    assert cmd.correlation_id == _CORR_ID


@pytest.mark.asyncio
async def test_rule_5_a_resolve_resolved_threads_skipped() -> None:
    """Rule 5: A_RESOLVE with only resolved threads → SKIP (no command)."""
    pr = _pr(501)
    classified = [_classified(pr, EnumPRTrack.A_RESOLVE)]
    request = _make_request(classified)

    thread_data = {
        "reviewThreads": [
            {"isResolved": True, "comments": [{"id": "IC_resolved"}]},
        ]
    }
    mock_proc = _mock_proc(thread_data)
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        handler = HandlerTriageOrchestrator()
        output = await handler.handle(request)

    assert len(output.events) == 0


@pytest.mark.asyncio
async def test_rule_5_a_resolve_empty_threads_skips() -> None:
    """Rule 5: A_RESOLVE with no threads → SKIP."""
    pr = _pr(502)
    classified = [_classified(pr, EnumPRTrack.A_RESOLVE)]
    request = _make_request(classified)

    mock_proc = _mock_proc({"reviewThreads": []})
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        handler = HandlerTriageOrchestrator()
        output = await handler.handle(request)

    assert len(output.events) == 0


@pytest.mark.asyncio
async def test_rule_5_a_resolve_gh_failure_skips() -> None:
    """Rule 5: A_RESOLVE when gh fails → SKIP gracefully."""
    pr = _pr(503)
    classified = [_classified(pr, EnumPRTrack.A_RESOLVE)]
    request = _make_request(classified)

    mock_proc = _mock_proc({}, returncode=1)
    mock_proc.communicate = AsyncMock(return_value=(b"", b"auth error"))
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        handler = HandlerTriageOrchestrator()
        output = await handler.handle(request)

    assert len(output.events) == 0


# ---------------------------------------------------------------------------
# Rule 7: B_POLISH CONFLICTING + DIRTY → ModelConflictHunkCommand
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rule_7_b_polish_conflicting_dirty_emits_conflict_hunk() -> None:
    """Rule 7: B_POLISH + CONFLICTING + DIRTY → ModelConflictHunkCommand."""
    pr = _pr(700, mergeable="CONFLICTING", merge_state_status="DIRTY")
    classified = [_classified(pr, EnumPRTrack.B_POLISH)]
    request = _make_request(classified)

    call_count = 0

    async def fake_subprocess(*args: Any, **kwargs: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # _resolve_pr_refs call
            return _mock_proc(
                {
                    "headRefName": "feat/conflicting",
                    "baseRefName": "main",
                    "headRefOid": "sha700abc",
                }
            )
        # _resolve_conflict_files call
        return _mock_proc({"files": [{"path": "src/foo.py"}, {"path": "src/bar.py"}]})

    with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
        handler = HandlerTriageOrchestrator()
        output = await handler.handle(request)

    assert len(output.events) == 1
    cmd = output.events[0]
    assert isinstance(cmd, ModelConflictHunkCommand)
    assert cmd.pr_number == 700
    assert cmd.repo == "OmniNode-ai/omni_home"
    assert cmd.head_ref_name == "feat/conflicting"
    assert cmd.base_ref_name == "main"
    assert "src/foo.py" in cmd.conflict_files
    assert "src/bar.py" in cmd.conflict_files
    assert cmd.routing_policy is not None
    assert cmd.run_id == str(_RUN_ID)


@pytest.mark.asyncio
async def test_rule_7_refs_failure_skips() -> None:
    """Rule 7: CONFLICTING + DIRTY, refs resolution fails → SKIP."""
    pr = _pr(701, mergeable="CONFLICTING", merge_state_status="DIRTY")
    classified = [_classified(pr, EnumPRTrack.B_POLISH)]
    request = _make_request(classified)

    mock_proc = _mock_proc({}, returncode=1)
    mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        handler = HandlerTriageOrchestrator()
        output = await handler.handle(request)

    assert len(output.events) == 0


@pytest.mark.asyncio
async def test_rule_7_empty_conflict_files_still_emits() -> None:
    """Rule 7: conflict_files can be empty (command still emitted, LLM resolves later)."""
    pr = _pr(702, mergeable="CONFLICTING", merge_state_status="DIRTY")
    classified = [_classified(pr, EnumPRTrack.B_POLISH)]
    request = _make_request(classified)

    call_count = 0

    async def fake_subprocess(*args: Any, **kwargs: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _mock_proc(
                {
                    "headRefName": "feat/702",
                    "baseRefName": "main",
                    "headRefOid": "sha702",
                }
            )
        # files resolution fails gracefully
        return _mock_proc({}, returncode=1)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
        handler = HandlerTriageOrchestrator()
        output = await handler.handle(request)

    assert len(output.events) == 1
    cmd = output.events[0]
    assert isinstance(cmd, ModelConflictHunkCommand)
    assert cmd.conflict_files == []


# ---------------------------------------------------------------------------
# Rule 9: B_POLISH DIRTY (non-CONFLICTING) → ModelCiFixCommand
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rule_9_b_polish_dirty_emits_ci_fix() -> None:
    """Rule 9: B_POLISH + MERGEABLE + DIRTY → ModelCiFixCommand."""
    pr = _pr(900, mergeable="MERGEABLE", merge_state_status="DIRTY")
    classified = [_classified(pr, EnumPRTrack.B_POLISH)]
    request = _make_request(classified)

    checks_data = {
        "statusCheckRollup": [
            {
                "conclusion": "FAILURE",
                "name": "test-suite / pytest",
                "detailsUrl": "https://github.com/OmniNode-ai/omni_home/actions/runs/12345678",
            }
        ]
    }

    call_count = 0

    async def fake_subprocess(*args: Any, **kwargs: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        # Both _resolve_failing_run_id and _resolve_failing_job_name use same gh call
        return _mock_proc(checks_data)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
        handler = HandlerTriageOrchestrator()
        output = await handler.handle(request)

    assert len(output.events) == 1
    cmd = output.events[0]
    assert isinstance(cmd, ModelCiFixCommand)
    assert cmd.pr_number == 900
    assert cmd.repo == "OmniNode-ai/omni_home"
    assert cmd.run_id_github == "12345678"
    assert cmd.failing_job_name == "test-suite / pytest"
    assert cmd.routing_policy is not None
    assert cmd.run_id == str(_RUN_ID)


@pytest.mark.asyncio
async def test_rule_9_no_failing_run_skips() -> None:
    """Rule 9: B_POLISH DIRTY, no failing run ID resolved → SKIP."""
    pr = _pr(901, mergeable="MERGEABLE", merge_state_status="DIRTY")
    classified = [_classified(pr, EnumPRTrack.B_POLISH)]
    request = _make_request(classified)

    mock_proc = _mock_proc({"statusCheckRollup": []})
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        handler = HandlerTriageOrchestrator()
        output = await handler.handle(request)

    assert len(output.events) == 0


@pytest.mark.asyncio
async def test_rule_9_unknown_job_name_fallback() -> None:
    """Rule 9: failing job name unavailable → falling back to 'unknown'."""
    pr = _pr(902, mergeable="MERGEABLE", merge_state_status="DIRTY")
    classified = [_classified(pr, EnumPRTrack.B_POLISH)]
    request = _make_request(classified)

    checks_data = {
        "statusCheckRollup": [
            {
                "conclusion": "FAILURE",
                "detailsUrl": "https://github.com/OmniNode-ai/omni_home/actions/runs/99999",
                # no "name" or "context" key
            }
        ]
    }

    with patch("asyncio.create_subprocess_exec", return_value=_mock_proc(checks_data)):
        handler = HandlerTriageOrchestrator()
        output = await handler.handle(request)

    assert len(output.events) == 1
    cmd = output.events[0]
    assert isinstance(cmd, ModelCiFixCommand)
    assert cmd.failing_job_name == "unknown"


# ---------------------------------------------------------------------------
# Mixed: all 6 command types in a single 6-PR fixture
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_six_pr_fixture_all_command_types() -> None:
    """6-PR fixture covering all 6 emittable command types."""
    pr1 = _pr(
        100, merge_state_status="CLEAN", review_decision="APPROVED"
    )  # Rule 2 → AutoMergeArm
    pr2 = _pr(
        200, merge_state_status="BEHIND", review_decision="APPROVED"
    )  # Rule 3 → Rebase
    pr3 = _pr(
        300, merge_state_status="BLOCKED", required_checks_pass=False
    )  # Rule 6 → CiRerun
    pr4 = _pr(400)  # Rule 5 → ThreadReply (A_RESOLVE)
    pr5 = _pr(
        500, mergeable="CONFLICTING", merge_state_status="DIRTY"
    )  # Rule 7 → ConflictHunk
    pr6 = _pr(600, mergeable="MERGEABLE", merge_state_status="DIRTY")  # Rule 9 → CiFix

    classified = [
        _classified(pr1, EnumPRTrack.A_UPDATE),
        _classified(pr2, EnumPRTrack.A_UPDATE),
        _classified(pr3, EnumPRTrack.B_POLISH),
        _classified(pr4, EnumPRTrack.A_RESOLVE),
        _classified(pr5, EnumPRTrack.B_POLISH),
        _classified(pr6, EnumPRTrack.B_POLISH),
    ]
    request = _make_request(classified)

    call_count = 0

    async def fake_subprocess(*args: Any, **kwargs: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        # pr1 → graphql id
        if call_count == 1:
            return _mock_proc({"id": "PR_kwAAA", "headRefName": "feat/100"})
        # pr2 → refs
        if call_count == 2:
            return _mock_proc(
                {
                    "headRefName": "feat/200",
                    "baseRefName": "main",
                    "headRefOid": "sha200",
                }
            )
        # pr3 → statusCheckRollup
        if call_count == 3:
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
        # pr4 → reviewThreads
        if call_count == 4:
            return _mock_proc(
                {
                    "reviewThreads": [
                        {"isResolved": False, "comments": [{"id": "IC_thread400"}]}
                    ]
                }
            )
        # pr5 → refs (for ConflictHunk)
        if call_count == 5:
            return _mock_proc(
                {
                    "headRefName": "feat/500",
                    "baseRefName": "main",
                    "headRefOid": "sha500",
                }
            )
        # pr5 → files
        if call_count == 6:
            return _mock_proc({"files": [{"path": "src/conflict.py"}]})
        # pr6 → statusCheckRollup (run_id)
        if call_count == 7:
            return _mock_proc(
                {
                    "statusCheckRollup": [
                        {
                            "conclusion": "FAILURE",
                            "name": "test / ci",
                            "detailsUrl": "https://github.com/OmniNode-ai/omni_home/actions/runs/600600",
                        }
                    ]
                }
            )
        # pr6 → statusCheckRollup (job name)
        return _mock_proc(
            {
                "statusCheckRollup": [
                    {
                        "conclusion": "FAILURE",
                        "name": "test / ci",
                        "detailsUrl": "https://github.com/OmniNode-ai/omni_home/actions/runs/600600",
                    }
                ]
            }
        )

    with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
        handler = HandlerTriageOrchestrator()
        output = await handler.handle(request)

    assert len(output.events) == 6
    types = {type(e).__name__ for e in output.events}
    assert types == {
        "ModelAutoMergeArmCommand",
        "ModelRebaseCommand",
        "ModelCiRerunCommand",
        "ModelThreadReplyCommand",
        "ModelConflictHunkCommand",
        "ModelCiFixCommand",
    }


# ---------------------------------------------------------------------------
# Regression: Phase 1 rules unaffected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_phase1_rule_2_still_works() -> None:
    """Regression: Rule 2 still emits ModelAutoMergeArmCommand after Phase 2 changes."""
    pr = _pr(100, merge_state_status="CLEAN", review_decision="APPROVED")
    classified = [_classified(pr, EnumPRTrack.A_UPDATE)]
    request = _make_request(classified)

    mock_proc = _mock_proc({"id": "PR_kwXXXXXX", "headRefName": "feat/test"})
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        handler = HandlerTriageOrchestrator()
        output = await handler.handle(request)

    assert len(output.events) == 1
    assert isinstance(output.events[0], ModelAutoMergeArmCommand)
    assert output.events[0].total_prs == 1


@pytest.mark.asyncio
async def test_phase1_rule_6_still_works() -> None:
    """Regression: Rule 6 still emits ModelCiRerunCommand after Phase 2 changes."""
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
    assert isinstance(output.events[0], ModelCiRerunCommand)
    assert output.events[0].run_id_github == "99887766"


@pytest.mark.asyncio
async def test_phase1_draft_still_skipped() -> None:
    """Regression: Rule 1 (draft) still skips after Phase 2 changes."""
    pr = _pr(1, is_draft=True)
    classified = [_classified(pr, EnumPRTrack.A_RESOLVE)]
    request = _make_request(classified)

    handler = HandlerTriageOrchestrator()
    output = await handler.handle(request)
    assert len(output.events) == 0
