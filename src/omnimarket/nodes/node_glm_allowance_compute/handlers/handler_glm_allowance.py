# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How much of the GLM Coding Plan's window and week is left, and the chain that leaves (OMN-20287).

``HandlerGlmAllowance.handle(ModelGlmAllowanceRequest) -> ModelGlmAllowanceResult`` (definition-B).

From the recent usage, the cap refusals the provider returned, and the policy the deployment declared, this
computes what is left of the rolling window and of the weekly period, then consults them for the GLM rungs
of one escalation chain:

* a class the judged evals did not approve, or approved at a longer call budget than this call has, loses
  its GLM rungs;
* a recent cap refusal holds the GLM rungs back until it can have cleared (a rate refusal for the cooldown,
  a window refusal for the window, a week refusal until the weekly reset);
* a window or a week whose usable allowance (what is left less the reserve) cannot take the call's estimated
  credits loses the GLM rungs, so the work goes to the next rung instead of failing on the cap;
* when both periods still hold at least the preferred fraction, each GLM rung moves ahead of the first rung
  the overlay ranks as costlier, never ahead of a cheaper one.

No clock (``now`` is in the request), no I/O, no model. Nothing about the plan is packaged: the policy is a
field of the request.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from omnimarket.enums.enum_glm_allowance import (
    EnumGlmAllowanceVerdict,
    EnumGlmRefusalScope,
    EnumGlmSkipReason,
)
from omnimarket.models.glm_allowance.glm_credit_math import (
    credits_for_usage,
    unrated_models,
    week_reset,
    week_start,
    window_start,
)
from omnimarket.models.glm_allowance.model_glm_allowance_policy import (
    ModelGlmAllowancePolicy,
)
from omnimarket.models.glm_allowance.model_glm_usage_record import ModelGlmRefusalRecord
from omnimarket.nodes.node_glm_allowance_compute.models.model_glm_allowance import (
    ModelGlmAllowanceRequest,
    ModelGlmAllowanceResult,
    ModelGlmChainRung,
    ModelGlmSkippedRung,
    ModelGlmWindowState,
)


def _state(
    *, cap: float, used: float, reserve_fraction: float, resets_at: datetime | None
) -> ModelGlmWindowState:
    remaining = max(0.0, cap - used)
    reserve = cap * reserve_fraction
    return ModelGlmWindowState(
        cap=cap,
        used=used,
        remaining=remaining,
        reserve=reserve,
        usable=max(0.0, remaining - reserve),
        remaining_fraction=remaining / cap,
        resets_at=resets_at,
    )


def _refusal_until(
    policy: ModelGlmAllowancePolicy, refusal: ModelGlmRefusalRecord
) -> datetime:
    if refusal.scope is EnumGlmRefusalScope.WINDOW:
        return refusal.observed_at + timedelta(hours=policy.window_hours)
    if refusal.scope is EnumGlmRefusalScope.WEEK:
        return week_reset(policy, refusal.observed_at)
    return refusal.observed_at + timedelta(seconds=policy.rate_cooldown_s)


def _promote(rungs: tuple[ModelGlmChainRung, ...]) -> tuple[ModelGlmChainRung, ...]:
    """Move each GLM rung ahead of the first rung ranked costlier than it, never earlier than that."""
    ordered = list(rungs)
    for glm in [r for r in rungs if r.is_glm]:
        at = ordered.index(glm)
        costlier = next(
            (
                i
                for i, r in enumerate(ordered)
                if r.cost_rank > glm.cost_rank and i < at
            ),
            None,
        )
        if costlier is not None:
            ordered.insert(costlier, ordered.pop(at))
    return tuple(ordered)


class HandlerGlmAllowance:
    """Consult the plan's allowance for the GLM rungs of one chain."""

    def handle(self, request: ModelGlmAllowanceRequest) -> ModelGlmAllowanceResult:
        policy, now = request.policy, request.now
        counted = tuple(
            (u, credits_for_usage(policy, u))
            for u in request.usage
            if u.started_at <= now
        )
        in_window = [
            (u, c) for u, c in counted if u.started_at > window_start(policy, now)
        ]
        in_week = [
            (u, c) for u, c in counted if u.started_at >= week_start(policy, now)
        ]
        window = _state(
            cap=policy.window_credits,
            used=sum(c for _, c in in_window),
            reserve_fraction=policy.window_reserve_fraction,
            resets_at=(
                min(u.started_at for u, _ in in_window)
                + timedelta(hours=policy.window_hours)
                if in_window
                else None
            ),
        )
        week = _state(
            cap=policy.weekly_credits,
            used=sum(c for _, c in in_week),
            reserve_fraction=policy.week_reserve_fraction,
            resets_at=week_reset(policy, now),
        )
        estimate = (
            request.estimated_credits
            if request.estimated_credits is not None
            else policy.call_reserve_credits
        )
        active = [
            until
            for until in (
                _refusal_until(policy, r)
                for r in request.refusals
                if r.observed_at <= now
            )
            if until > now
        ]
        blocked_until = max(active) if active else None

        reason = self._skip_reason(request, window, week, estimate, blocked_until)
        glm = tuple(r for r in request.chain if r.is_glm)
        if not glm:
            verdict = EnumGlmAllowanceVerdict.NO_GLM_RUNG
            ordered = request.chain
            skipped: tuple[ModelGlmSkippedRung, ...] = ()
        elif reason is not None:
            verdict = EnumGlmAllowanceVerdict.SKIP_GLM
            ordered = tuple(r for r in request.chain if not r.is_glm)
            skipped = tuple(
                ModelGlmSkippedRung(rung=r.name, reason=reason) for r in glm
            )
        elif (
            window.remaining_fraction >= policy.prefer_min_remaining_fraction
            and week.remaining_fraction >= policy.prefer_min_remaining_fraction
        ):
            verdict = EnumGlmAllowanceVerdict.PREFER_GLM
            ordered = _promote(request.chain)
            skipped = ()
        else:
            verdict = EnumGlmAllowanceVerdict.ALLOW_GLM
            ordered = request.chain
            skipped = ()
        return ModelGlmAllowanceResult(
            evaluated_at=now,
            provider_id=policy.provider_id,
            task_class=request.task_class,
            verdict=verdict,
            window=window,
            week=week,
            estimated_credits=estimate,
            blocked_until=blocked_until,
            ordered_rungs=tuple(r.name for r in ordered),
            skipped=skipped,
            unrated_models=unrated_models(policy, request.usage),
        )

    @staticmethod
    def _skip_reason(
        request: ModelGlmAllowanceRequest,
        window: ModelGlmWindowState,
        week: ModelGlmWindowState,
        estimate: float,
        blocked_until: datetime | None,
    ) -> EnumGlmSkipReason | None:
        approval = request.policy.approval_for(request.task_class)
        if approval is None:
            return EnumGlmSkipReason.CLASS_NOT_APPROVED
        if approval.min_budget_s is not None and (
            request.budget_s is None or request.budget_s < approval.min_budget_s
        ):
            return EnumGlmSkipReason.BUDGET_BELOW_CLASS_MINIMUM
        if blocked_until is not None:
            return EnumGlmSkipReason.PROVIDER_REFUSAL_COOLDOWN
        if window.usable < estimate:
            return EnumGlmSkipReason.WINDOW_NEAR_CAP
        if week.usable < estimate:
            return EnumGlmSkipReason.WEEK_NEAR_CAP
        return None


__all__ = ["HandlerGlmAllowance"]
