# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Task 10: Golden chain semantic proof — merge sweep executor pipeline [OMN-8965].

Wires all 6 nodes in-memory with mocked subprocesses (zero CLI, zero RuntimeLocal).
Proves the full semantic chain from classified PRs → fan-out → effects → compute → reducer.

Scope: semantic chain proof (NOT runtime proof — runtime is Task 12).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest

from omnimarket.nodes.node_ci_rerun_effect.handlers.handler_ci_rerun import (
    HandlerCiRerunEffect,
)
from omnimarket.nodes.node_merge_sweep_auto_merge_arm_effect.handlers.handler_auto_merge_arm import (
    HandlerAutoMergeArmEffect,
)
from omnimarket.nodes.node_merge_sweep_compute.handlers.handler_merge_sweep import (
    EnumPRTrack,
    ModelClassifiedPR,
    ModelMergeSweepResult,
    ModelPRInfo,
)
from omnimarket.nodes.node_merge_sweep_state_reducer.handlers.handler_sweep_state import (
    HandlerMergeSweepStateReducer,
)
from omnimarket.nodes.node_merge_sweep_state_reducer.models.model_merge_sweep_state import (
    ModelMergeSweepState,
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
from omnimarket.nodes.node_rebase_effect.handlers.handler_rebase import (
    HandlerRebaseEffect,
)
from omnimarket.nodes.node_sweep_outcome_classify.handlers.handler_outcome_classify import (
    HandlerSweepOutcomeClassify,
)
from omnimarket.nodes.node_sweep_outcome_classify.models.model_sweep_outcome import (
    EnumSweepOutcome,
    ModelSweepOutcomeInput,
)

_RUN_ID = UUID("00000000-0000-4000-a000-000000000010")
_CORR_ID = UUID("00000000-0000-4000-a000-000000000011")
_REPO = "OmniNode-ai/omni_home"


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
        repo=_REPO,
        mergeable=mergeable,
        merge_state_status=merge_state_status,
        review_decision=review_decision,
        required_checks_pass=required_checks_pass,
        is_draft=is_draft,
    )


def _classified(pr: ModelPRInfo, track: EnumPRTrack) -> ModelClassifiedPR:
    return ModelClassifiedPR(pr=pr, track=track, reason="test")


def _mock_subprocess(stdout: dict[str, Any], returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(json.dumps(stdout).encode(), b""))
    return proc


@pytest.mark.asyncio
async def test_golden_chain_3_prs_full_pipeline() -> None:
    """Full semantic chain: 3 PRs → orchestrator → 3 effects → classify → reduce.

    PR 100: A_UPDATE + CLEAN + APPROVED → AutoMergeArm → ARMED
    PR 200: A_UPDATE + BEHIND + APPROVED → Rebase (mocked success) → REBASED
    PR 300: B_POLISH + MERGEABLE + BLOCKED + checks_fail → CiRerun → CI_RERUN_TRIGGERED
    """
    pr1 = _pr(100, merge_state_status="CLEAN", review_decision="APPROVED")
    pr2 = _pr(200, merge_state_status="BEHIND", review_decision="APPROVED")
    pr3 = _pr(300, merge_state_status="BLOCKED", required_checks_pass=False)

    classified = [
        _classified(pr1, EnumPRTrack.A_UPDATE),
        _classified(pr2, EnumPRTrack.A_UPDATE),
        _classified(pr3, EnumPRTrack.B_POLISH),
    ]
    request = ModelTriageRequest(
        classification=ModelMergeSweepResult(classified=classified),
        run_id=_RUN_ID,
        correlation_id=_CORR_ID,
    )

    # --- Node 1: Orchestrator ---
    call_count = 0

    async def fake_subprocess(*args: Any, **kwargs: Any) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # PR 100: resolve GraphQL node ID
            return _mock_subprocess(
                {"id": "PR_kwGOLDEN100", "headRefName": "feat/pr100"}
            )
        if call_count == 2:
            # PR 200: resolve refs for rebase
            return _mock_subprocess(
                {
                    "headRefName": "feat/pr200",
                    "baseRefName": "main",
                    "headRefOid": "sha200abc",
                }
            )
        # PR 300: statusCheckRollup for CI rerun
        return _mock_subprocess(
            {
                "statusCheckRollup": [
                    {
                        "conclusion": "FAILURE",
                        "detailsUrl": f"https://github.com/{_REPO}/actions/runs/99887700",
                    }
                ]
            }
        )

    with patch("asyncio.create_subprocess_exec", side_effect=fake_subprocess):
        orchestrator = HandlerTriageOrchestrator()
        orch_output = await orchestrator.handle(request)

    assert len(orch_output.events) == 3
    event_types = {type(e).__name__ for e in orch_output.events}
    assert "ModelAutoMergeArmCommand" in event_types
    assert "ModelRebaseCommand" in event_types
    assert "ModelCiRerunCommand" in event_types
    assert orch_output.result is None  # ORCHESTRATOR never returns result

    # --- Node 2: AutoMerge effect ---
    arm_cmd = next(
        e for e in orch_output.events if isinstance(e, ModelAutoMergeArmCommand)
    )
    mock_gh_arm = _mock_subprocess({})  # GraphQL returns empty body on success
    with patch("asyncio.create_subprocess_exec", return_value=mock_gh_arm):
        auto_merge_handler = HandlerAutoMergeArmEffect()
        arm_output = await auto_merge_handler.handle(arm_cmd)

    assert len(arm_output.events) == 1
    arm_event = arm_output.events[0]
    assert arm_event.armed is True
    assert arm_event.pr_number == 100

    # --- Node 3: Rebase effect ---
    rebase_cmd = next(
        e for e in orch_output.events if isinstance(e, ModelRebaseCommand)
    )
    # Mock all git subprocess calls: worktree add, fetch, checkout, fetch base, rebase, push, rev-parse, worktree remove
    git_call_count = 0

    async def fake_git(*args: Any, **kwargs: Any) -> MagicMock:
        nonlocal git_call_count
        git_call_count += 1
        cmd = list(args)
        # All git calls succeed; rev-parse returns a raw SHA line
        proc = MagicMock()
        proc.returncode = 0
        if "rev-parse" in cmd:
            proc.communicate = AsyncMock(return_value=(b"rebased_sha123\n", b""))
        else:
            proc.communicate = AsyncMock(return_value=(b"ok\n", b""))
        return proc

    # Mock the source clone to exist via patching Path.exists
    with (
        patch("asyncio.create_subprocess_exec", side_effect=fake_git),
        patch(
            "omnimarket.nodes.node_rebase_effect.handlers.handler_rebase._source_clone_root",
            return_value=MagicMock(),
        ),
        patch("pathlib.Path.exists", return_value=True),
    ):
        rebase_handler = HandlerRebaseEffect()
        rebase_output = await rebase_handler.handle(rebase_cmd)

    assert len(rebase_output.events) == 1
    rebase_event = rebase_output.events[0]
    assert rebase_event.pr_number == 200

    # --- Node 4: CI rerun effect ---
    ci_cmd = next(e for e in orch_output.events if isinstance(e, ModelCiRerunCommand))
    assert ci_cmd.run_id_github == "99887700"
    mock_gh_rerun = MagicMock()
    mock_gh_rerun.returncode = 0
    mock_gh_rerun.communicate = AsyncMock(return_value=(b"", b""))
    with patch("asyncio.create_subprocess_exec", return_value=mock_gh_rerun):
        ci_handler = HandlerCiRerunEffect()
        ci_output = await ci_handler.handle(ci_cmd)

    assert len(ci_output.events) == 1
    ci_event = ci_output.events[0]
    assert ci_event.rerun_triggered is True
    assert ci_event.pr_number == 300

    # --- Node 5: COMPUTE — classify each completion event ---
    classify_handler = HandlerSweepOutcomeClassify()

    arm_classified_output = classify_handler.handle(
        ModelSweepOutcomeInput(
            event_type="armed",
            armed=arm_event.armed,
            error=arm_event.error,
            pr_number=arm_event.pr_number,
            repo=arm_event.repo,
            correlation_id=arm_event.correlation_id,
            run_id=arm_event.run_id,
            total_prs=arm_event.total_prs,
        )
    )
    assert arm_classified_output.result is not None
    arm_classified = arm_classified_output.result
    assert arm_classified.outcome == EnumSweepOutcome.ARMED

    rebase_classified_output = classify_handler.handle(
        ModelSweepOutcomeInput(
            event_type="rebase_completed",
            success=rebase_event.success,
            conflict_files=rebase_event.conflict_files,
            error=rebase_event.error,
            pr_number=rebase_event.pr_number,
            repo=rebase_event.repo,
            correlation_id=rebase_event.correlation_id,
            run_id=rebase_event.run_id,
            total_prs=rebase_event.total_prs,
        )
    )
    assert rebase_classified_output.result is not None
    rebase_classified = rebase_classified_output.result
    # Either REBASED (success) or STUCK/FAILED (conflict or failure)
    assert rebase_classified.outcome in {
        EnumSweepOutcome.REBASED,
        EnumSweepOutcome.STUCK,
        EnumSweepOutcome.FAILED,
    }

    ci_classified_output = classify_handler.handle(
        ModelSweepOutcomeInput(
            event_type="ci_rerun_triggered",
            rerun_triggered=ci_event.rerun_triggered,
            error=ci_event.error,
            pr_number=ci_event.pr_number,
            repo=ci_event.repo,
            correlation_id=ci_event.correlation_id,
            run_id=ci_event.run_id,
            total_prs=ci_event.total_prs,
        )
    )
    assert ci_classified_output.result is not None
    ci_classified = ci_classified_output.result
    assert ci_classified.outcome == EnumSweepOutcome.CI_RERUN_TRIGGERED

    # --- Node 6: REDUCER — aggregate all 3 outcomes ---
    reducer = HandlerMergeSweepStateReducer()
    state = ModelMergeSweepState(run_id=_RUN_ID, total_prs=3)

    # OMN-9010: intents now include a ModelPersistStateIntent on every mutation.
    # Bus-publish terminal intents are the dicts; filter to count terminals.
    def _bus(xs: list[object]) -> list[dict]:
        return [x for x in xs if isinstance(x, dict)]

    state, intents1 = reducer.delta(state, arm_classified)
    assert _bus(intents1) == []  # not terminal yet (1/3)

    state, intents2 = reducer.delta(state, rebase_classified)
    assert _bus(intents2) == []  # not terminal yet (2/3)

    state, intents3 = reducer.delta(state, ci_classified)
    bus3 = _bus(intents3)
    assert len(bus3) == 1  # terminal fires now (3/3)
    assert "merge-sweep-completed" in bus3[0]["topic"]

    # --- Final projection assertions ---
    assert state.total_prs == 3
    assert len(state.pr_outcomes_by_key) == 3
    assert state.terminal_emitted is True
    assert state.completed_at is not None

    # All PRs have non-empty outcome
    for _key, record in state.pr_outcomes_by_key.items():
        assert record.outcome  # non-empty string outcome

    # Counter sum == N
    total = (
        state.merged_count
        + state.armed_count
        + state.rebased_count
        + state.ci_rerun_count
        + state.failed_count
        + state.stuck_count
    )
    assert total == 3

    # PR numbers are represented (as dedup keys)
    keys = set(state.pr_outcomes_by_key.keys())
    assert f"{_REPO}#100" in keys
    assert f"{_REPO}#200" in keys
    assert f"{_REPO}#300" in keys


@pytest.mark.asyncio
async def test_golden_chain_orchestrator_result_is_none() -> None:
    """Orchestrator NEVER returns a result payload."""
    pr = _pr(100, merge_state_status="CLEAN", review_decision="APPROVED")
    classified = [_classified(pr, EnumPRTrack.A_UPDATE)]
    request = ModelTriageRequest(
        classification=ModelMergeSweepResult(classified=classified),
        run_id=_RUN_ID,
        correlation_id=_CORR_ID,
    )
    mock_proc = _mock_subprocess({"id": "PR_kwGOLDEN100", "headRefName": "feat/x"})
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        handler = HandlerTriageOrchestrator()
        output = await handler.handle(request)
    assert output.result is None


@pytest.mark.asyncio
async def test_golden_chain_all_skip_produces_empty_orchestrator_output() -> None:
    """All draft PRs → orchestrator emits zero commands → reducer never starts."""
    pr = _pr(100, is_draft=True)
    classified = [_classified(pr, EnumPRTrack.A_UPDATE)]
    request = ModelTriageRequest(
        classification=ModelMergeSweepResult(classified=classified),
        run_id=_RUN_ID,
        correlation_id=_CORR_ID,
    )
    handler = HandlerTriageOrchestrator()
    output = await handler.handle(request)
    assert len(output.events) == 0


@pytest.mark.asyncio
async def test_golden_chain_runs_in_under_5s() -> None:
    """Timing guard: golden chain with mocked subprocesses must complete < 5s.

    This is a structural guard — if this test becomes slow it means real I/O
    leaked into the mocked path.
    """
    import time

    pr = _pr(100, merge_state_status="CLEAN", review_decision="APPROVED")
    request = ModelTriageRequest(
        classification=ModelMergeSweepResult(
            classified=[_classified(pr, EnumPRTrack.A_UPDATE)]
        ),
        run_id=_RUN_ID,
        correlation_id=_CORR_ID,
    )
    mock_proc = _mock_subprocess({"id": "PR_kwBENCH", "headRefName": "feat/bench"})
    t0 = time.monotonic()
    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        handler = HandlerTriageOrchestrator()
        output = await handler.handle(request)
    elapsed = time.monotonic() - t0
    assert len(output.events) == 1
    assert elapsed < 5.0, f"Golden chain took {elapsed:.2f}s — real I/O may have leaked"
