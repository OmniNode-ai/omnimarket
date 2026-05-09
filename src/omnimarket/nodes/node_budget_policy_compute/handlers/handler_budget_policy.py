"""Pure deterministic budget policy evaluator — no I/O, no side effects."""

from __future__ import annotations

from omnimarket.nodes.node_budget_policy_compute.models.model_budget_policy_enums import (
    EnumBudgetAction,
    EnumTaskPriority,
)
from omnimarket.nodes.node_budget_policy_compute.models.model_budget_policy_request import (
    ModelBudgetPolicyRequest,
)
from omnimarket.nodes.node_budget_policy_compute.models.model_budget_policy_result import (
    ModelBudgetPolicyResult,
)

# Fraction of the limit at which WARN is triggered (applies to all dimensions).
_WARN_THRESHOLD = 0.80

# CRITICAL tasks raise the hard-abort ceiling to 120% of the declared limit,
# allowing burst capacity before a hard stop.  All other priorities hard-abort
# when any dimension strictly exceeds 100% (ratio > 1.0).
_ABORT_MULTIPLIER_CRITICAL = 1.20

_DIMENSION_LABELS = {
    "tokens": "tokens",
    "cost_usd": "cost_usd",
    "elapsed_time_s": "elapsed_time_s",
}


def _abort_multiplier(priority: EnumTaskPriority) -> float:
    if priority == EnumTaskPriority.CRITICAL:
        return _ABORT_MULTIPLIER_CRITICAL
    return 1.0


def _usage_ratios(request: ModelBudgetPolicyRequest) -> dict[str, float]:
    u = request.current_usage
    lim = request.budget_limits
    return {
        "tokens": u.tokens / lim.max_tokens,
        "cost_usd": u.cost_usd / lim.max_cost_usd,
        "elapsed_time_s": u.elapsed_time_s / lim.max_time_s,
    }


class HandlerBudgetPolicy:
    """Evaluate context usage against declared thresholds and return an EnumBudgetAction."""

    def handle(self, request: ModelBudgetPolicyRequest) -> ModelBudgetPolicyResult:
        ratios = _usage_ratios(request)
        abort_mult = _abort_multiplier(request.task_priority)

        # Classify each dimension.
        # throttle = at the declared limit (ratio in [1.0, abort_mult))
        # abort    = strictly exceeds the abort ceiling (ratio >= abort_mult)
        aborted: list[str] = []
        throttled: list[str] = []
        warned: list[str] = []

        for dim, ratio in ratios.items():
            if ratio > abort_mult:
                aborted.append(dim)
            elif ratio >= 1.0:
                throttled.append(dim)
            elif ratio >= _WARN_THRESHOLD:
                warned.append(dim)

        if aborted:
            exceeded = aborted + throttled + warned
            reason = (
                f"Hard limit exceeded on: {', '.join(aborted)}. "
                f"Task priority={request.task_priority.value}, "
                f"abort multiplier={abort_mult}."
            )
            return ModelBudgetPolicyResult(
                action=EnumBudgetAction.ABORT,
                reason=reason,
                dimensions_exceeded=exceeded,
                recommended_action="Stop task immediately and release resources.",
            )

        if throttled:
            exceeded = throttled + warned
            reason = (
                f"At or above limit on: {', '.join(throttled)}. "
                f"Task priority={request.task_priority.value}."
            )
            return ModelBudgetPolicyResult(
                action=EnumBudgetAction.THROTTLE,
                reason=reason,
                dimensions_exceeded=exceeded,
                recommended_action=(
                    "Reduce output quality or speed to stay within budget. "
                    "Consider checkpointing progress."
                ),
            )

        if warned:
            reason = (
                f"Approaching limit (>{int(_WARN_THRESHOLD * 100)}%) on: "
                f"{', '.join(warned)}. "
                f"Task priority={request.task_priority.value}."
            )
            return ModelBudgetPolicyResult(
                action=EnumBudgetAction.WARN,
                reason=reason,
                dimensions_exceeded=warned,
                recommended_action=(
                    "Monitor usage closely. Prepare to throttle if consumption continues."
                ),
            )

        return ModelBudgetPolicyResult(
            action=EnumBudgetAction.CONTINUE,
            reason="All dimensions within budget.",
            dimensions_exceeded=[],
            recommended_action="No action required.",
        )


__all__ = ["HandlerBudgetPolicy"]
