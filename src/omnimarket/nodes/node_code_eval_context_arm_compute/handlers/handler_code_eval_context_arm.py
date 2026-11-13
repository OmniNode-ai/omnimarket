# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Context-arm select and judge for code-eval context scoring (OMN-23629).

``select`` decides whether an arm scores the task alone (no pack) or requests a
context pack bounded by the overlay's budget and the backend's remaining
grounded-input room. ``judge`` measures the pack's answer against the arm's
required tiers, tier floor, exemplar line and the backend's ceiling. No clock,
no I/O, no randomness, no value of its own: the overlay travels on the request.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.models.context.model_context_caller_scope import (
    ModelContextCallerScope,
)

from omnimarket.models.context_pack_orchestrator import (
    EnumContextPackOrchestratorStatus,
    ModelContextPackBuilt,
    ModelContextPackRequest,
)
from omnimarket.nodes.node_code_eval_context_arm_compute.models.model_code_eval_context_arm import (
    EnumContextArmDecision,
    EnumContextArmOperation,
    EnumContextArmReason,
    ModelCodeEvalContextArmRequest,
    ModelCodeEvalContextArmResult,
    ModelContextArmSpec,
)

EXEMPLAR_LINE = re.compile(r"merged (?P<when>\S+) as (?P<sha>[0-9a-f]{12}) on ")


def _lane_kind(request: ModelCodeEvalContextArmRequest) -> str | None:
    """Lane kind from the task, falling back to the overlay's archetype mapping."""
    if request.task.lane_kind:
        return request.task.lane_kind
    if request.task.node_archetype is None:
        return None
    return request.overlay.archetype_lane_kind.get(request.task.node_archetype)


def _pack_budget(request: ModelCodeEvalContextArmRequest) -> int:
    """Pack budget bounded by the overlay's budget and the backend's remaining room."""
    room = (
        request.backend.max_grounded_input_tokens
        - request.backend.composed_request_tokens
    )
    return min(request.overlay.pack_budget_tokens, room)


def _included_factors(pack_answer: ModelContextPackBuilt) -> set[EnumContextFactor]:
    """Factors the judged pack carries."""
    return {tier.factor for tier in pack_answer.tier_statuses if tier.included}


def _tier_values(pack_answer: ModelContextPackBuilt, included: bool) -> tuple[str, ...]:
    """Factor values of the pack's tiers in tier order, filtered by inclusion."""
    return tuple(
        tier.factor.value
        for tier in pack_answer.tier_statuses
        if tier.included == included
    )


def _included_tiers(pack_answer: ModelContextPackBuilt) -> tuple[str, ...]:
    """Factor values of the tiers the judged pack includes, in tier order."""
    return _tier_values(pack_answer, True)


def _unresolved_tiers(pack_answer: ModelContextPackBuilt) -> tuple[str, ...]:
    """Factor values of the tiers the judged pack left out."""
    return _tier_values(pack_answer, False)


def _excluded_by_budget(pack_answer: ModelContextPackBuilt) -> tuple[str, ...]:
    """Factor values the judged pack's budget excluded."""
    return tuple(factor.value for factor in pack_answer.excluded_by_budget)


def _included_token_estimate(pack_answer: ModelContextPackBuilt) -> int:
    """Tokens of the tiers the judged pack carries."""
    return sum(
        tier.token_estimate for tier in pack_answer.tier_statuses if tier.included
    )


def _required_tiers_missing(
    spec: ModelContextArmSpec, included: set[EnumContextFactor]
) -> tuple[EnumContextFactor, ...]:
    """The arm's required factors the pack does not carry."""
    return tuple(
        tier for tier in spec.factor_arm().required_factors if tier not in included
    )


def _tier_status(pack_answer: ModelContextPackBuilt, factor: EnumContextFactor) -> str:
    """Status of one named tier in the judged pack answer."""
    for tier in pack_answer.tier_statuses:
        if tier.factor is factor:
            return tier.status.value
    return "unknown"


def _exemplar_match(
    pack_answer: ModelContextPackBuilt,
) -> tuple[str, str] | None:
    """(when, sha) of the exemplar line the pack carries, or None when no line verifies."""
    for message in pack_answer.messages:
        if not message.text.startswith("[exemplar:"):
            continue
        match = EXEMPLAR_LINE.search(message.text)
        if match is not None:
            return match["when"], match["sha"]
    return None


class HandlerCodeEvalContextArm:
    """Deterministic context-arm decisions over overlay specs, backend ceilings and pack answers."""

    def handle(
        self, request: ModelCodeEvalContextArmRequest
    ) -> ModelCodeEvalContextArmResult:
        """Decide one context arm for one code-eval task."""
        spec = request.overlay.context_arms.get(request.arm)
        if request.operation is EnumContextArmOperation.SELECT:
            return self._select(request, spec)
        return self._judge(request, spec)

    def _select(
        self, request: ModelCodeEvalContextArmRequest, spec: ModelContextArmSpec | None
    ) -> ModelCodeEvalContextArmResult:
        if spec is None:
            return self._result(
                request,
                decision=EnumContextArmDecision.REFUSED,
                reason=EnumContextArmReason.ARM_NOT_IN_OVERLAY,
                detail=f"arm {request.arm} is not named by the overlay",
            )
        if spec.profile is None:
            return self._result(
                request,
                decision=EnumContextArmDecision.NO_PACK_NEEDED,
                reason=EnumContextArmReason.OK,
                detail="the arm scores the task alone, so no context pack is needed",
                token_estimate=0,
                token_estimate_basis="none",
            )
        lane_kind = _lane_kind(request)
        if lane_kind is None:
            return self._result(
                request,
                decision=EnumContextArmDecision.REFUSED,
                reason=EnumContextArmReason.NO_LANE_KIND,
                detail="neither the task nor the overlay's archetype mapping names a lane kind",
            )
        room = (
            request.backend.max_grounded_input_tokens
            - request.backend.composed_request_tokens
        )
        if room <= 0:
            return self._result(
                request,
                decision=EnumContextArmDecision.REFUSED,
                reason=EnumContextArmReason.NO_ROOM,
                detail=f"no room under the grounded-input ceiling: {room} tokens remain",
            )
        budget = _pack_budget(request)
        pack_request = ModelContextPackRequest(
            profile_name=spec.profile,
            caller_scope=ModelContextCallerScope(
                repository=request.task.repository,
                node_archetype=request.task.node_archetype,
                lane_kind=lane_kind,
            ),
            token_budget_override=budget,
            tier_revisions=dict.fromkeys(
                spec.pinned_tiers, request.task.base_commit_time
            ),
        )
        return self._result(
            request,
            decision=EnumContextArmDecision.REQUEST_PACK,
            reason=EnumContextArmReason.OK,
            detail="request a context pack bounded by the overlay's budget and the backend's remaining room",
            pack_request=pack_request,
            pack_budget_tokens=budget,
            token_estimate=budget,
            token_estimate_basis="budget_ceiling",
        )

    def _judge(
        self, request: ModelCodeEvalContextArmRequest, spec: ModelContextArmSpec | None
    ) -> ModelCodeEvalContextArmResult:
        pack_answer = request.pack_answer

        if spec is None:
            return self._result(
                request,
                decision=EnumContextArmDecision.REFUSED,
                reason=EnumContextArmReason.ARM_NOT_IN_OVERLAY,
                detail=f"arm {request.arm} is not named by the overlay",
            )
        if spec.profile is None:
            if pack_answer is None:
                return self._result(
                    request,
                    decision=EnumContextArmDecision.SATISFIED,
                    reason=EnumContextArmReason.OK,
                    detail="the arm scores the task alone and no pack was answered",
                    token_estimate=0,
                    token_estimate_basis="none",
                )
            return self._result(
                request,
                decision=EnumContextArmDecision.NOT_SATISFIED,
                reason=EnumContextArmReason.UNEXPECTED_PACK,
                detail="the arm scores the task alone but a pack answer was given",
                unscorable_context=True,
                pack_answer=pack_answer,
            )
        if pack_answer is None:
            return self._result(
                request,
                decision=EnumContextArmDecision.NOT_SATISFIED,
                reason=EnumContextArmReason.PACK_NOT_ANSWERED,
                detail="the context pack has not been answered yet",
                unscorable_context=True,
            )
        if pack_answer.status is not EnumContextPackOrchestratorStatus.OK:
            failure = (
                pack_answer.failure_class.value
                if pack_answer.failure_class is not None
                else "none"
            )
            return self._result(
                request,
                decision=EnumContextArmDecision.NOT_SATISFIED,
                reason=EnumContextArmReason.PACK_FAILED,
                detail=f"the context pack build finished {pack_answer.status.value} with failure class {failure}",
                unscorable_context=True,
                pack_answer=pack_answer,
            )
        if pack_answer.profile_name != spec.profile:
            return self._result(
                request,
                decision=EnumContextArmDecision.NOT_SATISFIED,
                reason=EnumContextArmReason.PROFILE_MISMATCH,
                detail=f"the pack was built for profile {pack_answer.profile_name}, not the arm's {spec.profile}",
                unscorable_context=True,
                pack_answer=pack_answer,
            )
        included = _included_factors(pack_answer)
        missing = _required_tiers_missing(spec, included)
        if missing:
            names = "; ".join(
                f"{tier.value} ({_tier_status(pack_answer, tier)})" for tier in missing
            )
            return self._result(
                request,
                decision=EnumContextArmDecision.NOT_SATISFIED,
                reason=EnumContextArmReason.REQUIRED_TIER_MISSING,
                detail=f"the pack does not carry the arm's required tier(s): {names}",
                unscorable_context=True,
                pack_answer=pack_answer,
            )
        if len(included) < spec.min_included_tiers:
            return self._result(
                request,
                decision=EnumContextArmDecision.NOT_SATISFIED,
                reason=EnumContextArmReason.TOO_FEW_TIERS,
                detail=f"the pack includes {len(included)} tiers, below the arm's floor of {spec.min_included_tiers}",
                unscorable_context=True,
                pack_answer=pack_answer,
            )

        exemplar_ref = None
        if EnumContextFactor.EXEMPLAR in included:
            match = _exemplar_match(pack_answer)
            if match is None:
                return self._result(
                    request,
                    decision=EnumContextArmDecision.NOT_SATISFIED,
                    reason=EnumContextArmReason.EXEMPLAR_NOT_VERIFIABLE,
                    detail="the exemplar tier is included but no merge line in the pack verifies an exemplar commit",
                    unscorable_context=True,
                    pack_answer=pack_answer,
                )
            when, sha = match
            exemplar_ref = sha
            if request.task.merge_commit.startswith(sha):
                return self._result(
                    request,
                    decision=EnumContextArmDecision.NOT_SATISFIED,
                    reason=EnumContextArmReason.EXEMPLAR_IS_TASK_MERGE_COMMIT,
                    detail=f"the exemplar commit {sha} is the task's own merge commit; the pack carries the answer",
                    unscorable_context=True,
                    exemplar_ref=sha,
                    pack_answer=pack_answer,
                )
            if datetime.fromisoformat(when) > datetime.fromisoformat(
                request.task.base_commit_time
            ):
                return self._result(
                    request,
                    decision=EnumContextArmDecision.NOT_SATISFIED,
                    reason=EnumContextArmReason.EXEMPLAR_AFTER_BASE,
                    detail=f"the exemplar merged at {when}, after the base commit time {request.task.base_commit_time}",
                    unscorable_context=True,
                    exemplar_ref=sha,
                    pack_answer=pack_answer,
                )

        token_estimate = _included_token_estimate(pack_answer)
        if (
            request.backend.composed_request_tokens + token_estimate
            > request.backend.max_grounded_input_tokens
        ):
            return self._result(
                request,
                decision=EnumContextArmDecision.REFUSED,
                reason=EnumContextArmReason.OVER_INPUT_CEILING,
                detail=(
                    f"the composed request ({request.backend.composed_request_tokens} tokens) plus the included tiers "
                    f"({token_estimate} tokens) exceeds the {request.backend.max_grounded_input_tokens}-token ceiling"
                ),
                token_estimate=token_estimate,
                token_estimate_basis="included_tiers",
                exemplar_ref=exemplar_ref,
                pack_answer=pack_answer,
            )
        return self._result(
            request,
            decision=EnumContextArmDecision.SATISFIED,
            reason=EnumContextArmReason.OK,
            detail="the pack answers the arm's required tiers, floor and exemplar, and the input fits the ceiling",
            token_estimate=token_estimate,
            token_estimate_basis="included_tiers",
            exemplar_ref=exemplar_ref,
            pack_answer=pack_answer,
        )

    def _result(
        self,
        request: ModelCodeEvalContextArmRequest,
        *,
        decision: EnumContextArmDecision,
        reason: EnumContextArmReason,
        detail: str,
        unscorable_context: bool = False,
        pack_request: ModelContextPackRequest | None = None,
        pack_budget_tokens: int | None = None,
        token_estimate: int = 0,
        token_estimate_basis: Literal["none", "budget_ceiling", "included_tiers"] = (
            "none"
        ),
        pack_answer: ModelContextPackBuilt | None = None,
        exemplar_ref: str | None = None,
    ) -> ModelCodeEvalContextArmResult:
        return ModelCodeEvalContextArmResult(
            correlation_id=request.correlation_id,
            operation=request.operation,
            arm=request.arm,
            decision=decision,
            reason=reason,
            detail=detail,
            unscorable_context=unscorable_context,
            pack_request=pack_request,
            pack_budget_tokens=pack_budget_tokens,
            token_estimate=token_estimate,
            token_estimate_basis=token_estimate_basis,
            included_tiers=_included_tiers(pack_answer) if pack_answer else (),
            excluded_by_budget=_excluded_by_budget(pack_answer) if pack_answer else (),
            unresolved_tiers=_unresolved_tiers(pack_answer) if pack_answer else (),
            final_pack_hash=pack_answer.final_pack_hash if pack_answer else None,
            exemplar_ref=exemplar_ref,
        )
