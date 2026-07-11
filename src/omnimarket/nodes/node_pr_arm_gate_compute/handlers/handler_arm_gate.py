# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPrArmGate — the merge-queue governor's sole ARM/WITHHOLD decider.

OMN-14151 (corrected arm-gate design). Pure COMPUTE — zero I/O, zero network
calls, no re-derivation of any fact it is handed. ARM requires every criterion
below to be POSITIVELY true; any ``None``/unknown/absent fact or policy value
WITHHOLDs. This is the single choke point the merge-queue governor's
"exactly one active arm path" invariant depends on: ``action_mode`` and
``kill_switch`` are folded into this same decision rather than checked by a
second, separately-bypassable guard.

Related:
    - OMN-14151: merge-queue governor arm-gate
    - docs/plans/2026-07-10-omn-14151-corrected-armgate-design.md
"""

from __future__ import annotations

import logging
from typing import Literal

from omnimarket.events.pr_arm_gate import (
    EnumArmActionMode,
    EnumArmDecision,
    ModelArmGateDecision,
    ModelArmGateRequest,
)

logger = logging.getLogger(__name__)

_REQUIRED_MERGE_STATE_STATUS = "CLEAN"
_REQUIRED_STATUS_CHECKS = "SUCCESS"

HandlerType = Literal["NODE_HANDLER"]
HandlerCategory = Literal["COMPUTE"]


class HandlerPrArmGate:
    """Fail-closed ARM/WITHHOLD decider. No I/O, no re-derivation of facts."""

    @property
    def handler_type(self) -> HandlerType:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> HandlerCategory:
        return "COMPUTE"

    async def handle(self, request: ModelArmGateRequest) -> ModelArmGateDecision:
        """Evaluate one candidate against the operator policy.

        ARM iff action_mode == ENFORCE AND not kill_switch AND is_draft is
        False AND coderabbit_unresolved == 0 AND merge_state_status == CLEAN
        AND status_checks == SUCCESS AND occ_companion_verified is True.
        Any missing/unknown fact or policy value contributes a WITHHOLD reason
        instead of being treated as satisfied.
        """
        candidate = request.candidate
        policy = request.policy
        reasons: list[str] = []

        if policy.action_mode is not EnumArmActionMode.ENFORCE:
            reasons.append(f"action_mode={policy.action_mode.value!r} (not enforce)")
        if policy.kill_switch:
            reasons.append("kill_switch_engaged")
        if candidate.is_draft is not False:
            reasons.append(f"is_draft={candidate.is_draft!r} (not confirmed False)")
        if candidate.coderabbit_unresolved != 0:
            reasons.append(
                f"coderabbit_unresolved={candidate.coderabbit_unresolved!r} (not confirmed 0)"
            )
        if candidate.merge_state_status != _REQUIRED_MERGE_STATE_STATUS:
            reasons.append(
                f"merge_state_status={candidate.merge_state_status!r} "
                f"(not {_REQUIRED_MERGE_STATE_STATUS!r})"
            )
        if candidate.status_checks != _REQUIRED_STATUS_CHECKS:
            reasons.append(
                f"status_checks={candidate.status_checks!r} "
                f"(not {_REQUIRED_STATUS_CHECKS!r})"
            )
        if candidate.occ_companion_verified is not True:
            reasons.append(
                f"occ_companion_verified={candidate.occ_companion_verified!r} "
                "(not confirmed True)"
            )

        decision = EnumArmDecision.WITHHOLD if reasons else EnumArmDecision.ARM
        if decision is EnumArmDecision.WITHHOLD:
            logger.info(
                "[ARM-GATE] WITHHOLD %s#%s reasons=%s",
                candidate.repo,
                candidate.pr_number,
                reasons,
            )
        return ModelArmGateDecision(
            repo=candidate.repo,
            pr_number=candidate.pr_number,
            decision=decision,
            withheld_reasons=tuple(reasons),
            priority_score=candidate.priority_hint,
        )


__all__: list[str] = ["HandlerPrArmGate"]
