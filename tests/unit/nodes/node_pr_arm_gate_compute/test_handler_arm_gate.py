# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerPrArmGate — the merge-queue governor's sole
ARM/WITHHOLD decider (OMN-14151).

Proves the fail-closed contract: ARM requires every criterion positively
true; any None/unknown/absent fact or policy value WITHHOLDs with a
machine-readable reason.
"""

from __future__ import annotations

import pytest

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

_REPO = "OmniNode-ai/omnimarket"
_PR_NUMBER = 42

_ENFORCE_POLICY = ModelArmGatePolicy(
    action_mode=EnumArmActionMode.ENFORCE, kill_switch=False
)
_ARM_READY_CANDIDATE = ModelArmCandidate(
    repo=_REPO,
    pr_number=_PR_NUMBER,
    is_draft=False,
    coderabbit_unresolved=0,
    merge_state_status="CLEAN",
    status_checks="SUCCESS",
    occ_companion_verified=True,
)


async def _decide(
    candidate: ModelArmCandidate, policy: ModelArmGatePolicy
) -> EnumArmDecision:
    handler = HandlerPrArmGate()
    decision = await handler.handle(
        ModelArmGateRequest(candidate=candidate, policy=policy)
    )
    return decision.decision


@pytest.mark.unit
@pytest.mark.asyncio
async def test_all_criteria_positive_arms() -> None:
    """Every criterion positively true -> ARM with zero withheld reasons."""
    handler = HandlerPrArmGate()
    decision = await handler.handle(
        ModelArmGateRequest(candidate=_ARM_READY_CANDIDATE, policy=_ENFORCE_POLICY)
    )
    assert decision.decision == EnumArmDecision.ARM
    assert decision.withheld_reasons == ()
    assert decision.repo == _REPO
    assert decision.pr_number == _PR_NUMBER


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_policy_is_report_only_withholds() -> None:
    """The default policy (report_only, kill_switch engaged) always WITHHOLDs,
    even with an otherwise arm-ready candidate."""
    handler = HandlerPrArmGate()
    decision = await handler.handle(
        ModelArmGateRequest(candidate=_ARM_READY_CANDIDATE, policy=ModelArmGatePolicy())
    )
    assert decision.decision == EnumArmDecision.WITHHOLD
    assert any("action_mode" in reason for reason in decision.withheld_reasons)
    assert any("kill_switch" in reason for reason in decision.withheld_reasons)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kill_switch_engaged_withholds_even_under_enforce() -> None:
    policy = ModelArmGatePolicy(action_mode=EnumArmActionMode.ENFORCE, kill_switch=True)
    decision = await _decide(_ARM_READY_CANDIDATE, policy)
    assert decision == EnumArmDecision.WITHHOLD


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("is_draft", [True, None])
async def test_is_draft_not_confirmed_false_withholds(is_draft: bool | None) -> None:
    candidate = _ARM_READY_CANDIDATE.model_copy(update={"is_draft": is_draft})
    decision = await _decide(candidate, _ENFORCE_POLICY)
    assert decision == EnumArmDecision.WITHHOLD


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("coderabbit_unresolved", [1, 5, None])
async def test_coderabbit_unresolved_not_confirmed_zero_withholds(
    coderabbit_unresolved: int | None,
) -> None:
    """None must WITHHOLD exactly like a nonzero count — never default to 0."""
    candidate = _ARM_READY_CANDIDATE.model_copy(
        update={"coderabbit_unresolved": coderabbit_unresolved}
    )
    decision = await _decide(candidate, _ENFORCE_POLICY)
    assert decision == EnumArmDecision.WITHHOLD


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "merge_state_status", ["DIRTY", "BLOCKED", "BEHIND", "UNKNOWN", None]
)
async def test_merge_state_status_not_clean_withholds(
    merge_state_status: str | None,
) -> None:
    candidate = _ARM_READY_CANDIDATE.model_copy(
        update={"merge_state_status": merge_state_status}
    )
    decision = await _decide(candidate, _ENFORCE_POLICY)
    assert decision == EnumArmDecision.WITHHOLD


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("status_checks", ["FAILURE", "PENDING", None])
async def test_status_checks_not_success_withholds(status_checks: str | None) -> None:
    candidate = _ARM_READY_CANDIDATE.model_copy(update={"status_checks": status_checks})
    decision = await _decide(candidate, _ENFORCE_POLICY)
    assert decision == EnumArmDecision.WITHHOLD


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("occ_companion_verified", [False, None])
async def test_occ_companion_not_confirmed_verified_withholds(
    occ_companion_verified: bool | None,
) -> None:
    candidate = _ARM_READY_CANDIDATE.model_copy(
        update={"occ_companion_verified": occ_companion_verified}
    )
    decision = await _decide(candidate, _ENFORCE_POLICY)
    assert decision == EnumArmDecision.WITHHOLD


@pytest.mark.unit
@pytest.mark.asyncio
async def test_withheld_reasons_enumerate_every_failed_criterion() -> None:
    """withheld_reasons is not a single collapsed string — every failing
    criterion is independently represented."""
    candidate = ModelArmCandidate(repo=_REPO, pr_number=_PR_NUMBER)  # all facts None
    handler = HandlerPrArmGate()
    decision = await handler.handle(
        ModelArmGateRequest(candidate=candidate, policy=ModelArmGatePolicy())
    )
    assert decision.decision == EnumArmDecision.WITHHOLD
    # action_mode, kill_switch, is_draft, coderabbit_unresolved,
    # merge_state_status, status_checks, occ_companion_verified.
    assert len(decision.withheld_reasons) == 7


@pytest.mark.unit
@pytest.mark.asyncio
async def test_priority_score_echoes_candidate_priority_hint() -> None:
    candidate = _ARM_READY_CANDIDATE.model_copy(update={"priority_hint": 7})
    handler = HandlerPrArmGate()
    decision = await handler.handle(
        ModelArmGateRequest(candidate=candidate, policy=_ENFORCE_POLICY)
    )
    assert decision.priority_score == 7
