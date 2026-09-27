# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Composes classify_head_checks' verdict (T8, OMN-19830) with read_head_checks'
run-attempt facts (T9, OMN-19826/OMN-19831) into node_pr_arm_gate_compute's
ARM/WITHHOLD decision.

Before this change, ``ModelArmCandidate.status_checks`` was the only checks
criterion, and no caller in the codebase ever positively populates it with a
genuine ``SUCCESS`` derived from real head-check facts -- the gate withholds
every arm regardless of whether the head is actually green. This test proves
the gate can resolve a real ARM verdict from ``classify_head_checks``' output
built over genuine (fake-transport-recorded-shape) check-run facts, including
one with a run attempt read back by ``read_head_checks`` (F7).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnimarket.events.pr_head_check.enum_head_check_verdict import (
    EnumHeadCheckVerdict,
)
from omnimarket.nodes.node_pr_arm_gate_compute.handlers.handler_arm_gate import (
    HandlerPrArmGate,
)
from omnimarket.nodes.node_pr_arm_gate_compute.models.model_arm_candidate import (
    ModelArmCandidate,
)
from omnimarket.nodes.node_pr_arm_gate_compute.models.model_arm_gate_decision import (
    EnumArmDecision,
)
from omnimarket.nodes.node_pr_arm_gate_compute.models.model_arm_gate_policy import (
    EnumArmActionMode,
    ModelArmGatePolicy,
)
from omnimarket.nodes.node_pr_arm_gate_compute.models.model_arm_gate_request import (
    ModelArmGateRequest,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_head_checks import (
    classify_head_checks,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_check_run_conclusion import (
    EnumCheckRunConclusion,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_check_run_status import (
    EnumCheckRunStatus,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_head_check_companion_state import (
    EnumHeadCheckCompanionState,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_pr_merge_state import (
    EnumPrMergeState,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_head_check_facts import (
    ModelHeadCheckFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_head_check_run import (
    ModelHeadCheckRun,
)

_REPO = "OmniNode-ai/omnimarket"
_PR_NUMBER = 42
_HEAD_SHA = "a" * 40
_NOW = datetime(2026, 9, 27, tzinfo=UTC)
_ENFORCE_POLICY = ModelArmGatePolicy(
    action_mode=EnumArmActionMode.ENFORCE, kill_switch=False
)

_ARM_READY_BASE = ModelArmCandidate(
    repo=_REPO,
    pr_number=_PR_NUMBER,
    is_draft=False,
    coderabbit_unresolved=0,
    merge_state_status="CLEAN",
    occ_companion_verified=True,
)


def _facts(checks: tuple[ModelHeadCheckRun, ...]) -> ModelHeadCheckFacts:
    return ModelHeadCheckFacts(
        repository=_REPO,
        pr_number=_PR_NUMBER,
        head_sha=_HEAD_SHA,
        base_ref="dev",
        observed_at=_NOW,
        checks=checks,
        companion_state=EnumHeadCheckCompanionState.NONE,
        merge_state=EnumPrMergeState.CLEAN,
        base_requires_up_to_date=False,
    )


async def _decide(candidate: ModelArmCandidate) -> EnumArmDecision:
    handler = HandlerPrArmGate()
    decision = await handler.handle(
        ModelArmGateRequest(candidate=candidate, policy=_ENFORCE_POLICY)
    )
    return decision.decision


@pytest.mark.unit
@pytest.mark.asyncio
async def test_green_head_check_verdict_arms() -> None:
    """A genuinely green classify_head_checks verdict, fed to the arm gate as
    head_check_verdict, arms -- with no status_checks string set at all."""
    facts = _facts(
        (
            ModelHeadCheckRun(
                name="ci",
                check_run_id=1,
                status=EnumCheckRunStatus.COMPLETED,
                conclusion=EnumCheckRunConclusion.SUCCESS,
                started_at=_NOW,
                required=True,
            ),
        )
    )
    verdict = classify_head_checks(facts)
    assert verdict.verdict is EnumHeadCheckVerdict.GREEN

    candidate = _ARM_READY_BASE.model_copy(
        update={"head_check_verdict": verdict.verdict}
    )
    assert await _decide(candidate) == EnumArmDecision.ARM


@pytest.mark.unit
@pytest.mark.asyncio
async def test_product_failed_head_check_verdict_withholds_with_reason() -> None:
    """A real product failure withholds, and the reason names the verdict --
    not a bare, unexplained WITHHOLD."""
    facts = _facts(
        (
            ModelHeadCheckRun(
                name="ci",
                check_run_id=1,
                status=EnumCheckRunStatus.COMPLETED,
                conclusion=EnumCheckRunConclusion.FAILURE,
                started_at=_NOW,
                required=True,
                failed_step="pytest",
                run_id=123,
                run_attempt=2,
            ),
        )
    )
    verdict = classify_head_checks(facts)
    assert verdict.verdict is EnumHeadCheckVerdict.PRODUCT_FAILED

    candidate = _ARM_READY_BASE.model_copy(
        update={"head_check_verdict": verdict.verdict}
    )
    handler = HandlerPrArmGate()
    decision = await handler.handle(
        ModelArmGateRequest(candidate=candidate, policy=_ENFORCE_POLICY)
    )
    assert decision.decision == EnumArmDecision.WITHHOLD
    assert any(
        "head_check_verdict" in reason and "product_failed" in reason
        for reason in decision.withheld_reasons
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_head_check_verdict_governs_over_a_stale_status_checks_string() -> None:
    """Once head_check_verdict is supplied it is the checks criterion --
    a stale/legacy status_checks string on the same candidate is not
    independently re-consulted, proving genuine composition rather than an
    additive second check."""
    candidate = _ARM_READY_BASE.model_copy(
        update={
            "status_checks": "FAILURE",
            "head_check_verdict": EnumHeadCheckVerdict.GREEN,
        }
    )
    assert await _decide(candidate) == EnumArmDecision.ARM


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pending_head_check_verdict_withholds() -> None:
    """PENDING (an unfinished required check) withholds -- never treated as
    a pass just because it is not a positive failure."""
    candidate = _ARM_READY_BASE.model_copy(
        update={"head_check_verdict": EnumHeadCheckVerdict.PENDING}
    )
    assert await _decide(candidate) == EnumArmDecision.WITHHOLD


@pytest.mark.unit
@pytest.mark.asyncio
async def test_absent_head_check_verdict_falls_back_to_legacy_status_checks() -> None:
    """No head_check_verdict supplied (every existing caller today) preserves
    the pre-existing status_checks-string behaviour exactly."""
    candidate = _ARM_READY_BASE.model_copy(update={"status_checks": "SUCCESS"})
    assert candidate.head_check_verdict is None
    assert await _decide(candidate) == EnumArmDecision.ARM

    withheld_candidate = _ARM_READY_BASE.model_copy(update={"status_checks": None})
    assert await _decide(withheld_candidate) == EnumArmDecision.WITHHOLD
