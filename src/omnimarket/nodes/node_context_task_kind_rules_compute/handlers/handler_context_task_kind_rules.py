# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerContextTaskKindRules: a profile's task-kind rules over a caller scope (OMN-21131, TC.8).

Pure and definition-B: ``handle(ModelContextTaskKindRulesRequest) -> ModelContextTaskKindRulesResult``,
no event envelope, no I/O. A rule is declared data over the typed caller scope (plan
section 4.4; port items AP-S4 and AP-S8): ``equals`` is exact, ``glob`` is a full-path
glob, a condition over a field the caller did not supply fails whether or not it is
negated, and an unknown operator, field or condition logic is a typed error, never a
silent false. An unmatched request is ``task_kind: unknown`` and admits only the
profile's unconditional factors; there is no "nothing matched, load everything" widening.
"""

from __future__ import annotations

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.models.context.model_context_caller_scope import (
    ModelContextCallerScope,
)

from omnimarket.enums.enum_context_task_kind_condition_logic import (
    EnumContextTaskKindConditionLogic,
)
from omnimarket.enums.enum_context_task_kind_operator import (
    EnumContextTaskKindOperator,
)
from omnimarket.models.context_pack.model_context_task_kind_condition import (
    PATH_SCOPE_FIELDS,
    ModelContextTaskKindCondition,
    path_glob_matches,
)
from omnimarket.models.context_pack.model_context_task_kind_rule import (
    TASK_KIND_UNKNOWN,
    ModelContextTaskKindRule,
)
from omnimarket.nodes.node_context_task_kind_rules_compute.models import (
    EnumContextTaskKindRulesStatus,
    ModelContextTaskKindConditionOutcome,
    ModelContextTaskKindRuleOutcome,
    ModelContextTaskKindRulesRequest,
    ModelContextTaskKindRulesResult,
)


class TaskKindRuleEvaluationError(ValueError):
    """A condition or rule that cannot be evaluated; reported, never read as false."""


def _scope_values(scope: ModelContextCallerScope, field: str) -> tuple[str, ...]:
    """The supplied values of ``field``; empty when the caller did not supply it."""
    if field not in ModelContextCallerScope.model_fields:
        raise TaskKindRuleEvaluationError(f"unknown caller-scope field {field!r}")
    raw = getattr(scope, field)
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    return tuple(raw)


def _evaluate_condition(
    index: int,
    condition: ModelContextTaskKindCondition,
    scope: ModelContextCallerScope,
) -> ModelContextTaskKindConditionOutcome:
    values = _scope_values(scope, condition.scope_field)
    operator = condition.operator
    if operator is EnumContextTaskKindOperator.EQUALS:
        held = any(value == condition.value for value in values)
    elif operator is EnumContextTaskKindOperator.GLOB:
        if condition.scope_field not in PATH_SCOPE_FIELDS:
            raise TaskKindRuleEvaluationError(
                f"operator glob on non-path field {condition.scope_field!r}"
            )
        try:
            held = any(path_glob_matches(condition.value, value) for value in values)
        except ValueError as exc:
            raise TaskKindRuleEvaluationError(str(exc)) from exc
    else:
        raise TaskKindRuleEvaluationError(f"unknown operator {operator!r}")
    absent = not values
    return ModelContextTaskKindConditionOutcome(
        index=index,
        scope_field=condition.scope_field,
        operator=operator,
        value=condition.value,
        negate=condition.negate,
        absent=absent,
        matched=False if absent else held != condition.negate,
    )


def _evaluate_rule(
    rule: ModelContextTaskKindRule, scope: ModelContextCallerScope
) -> ModelContextTaskKindRuleOutcome:
    outcomes = tuple(
        _evaluate_condition(index, condition, scope)
        for index, condition in enumerate(rule.conditions)
    )
    if not outcomes:
        raise TaskKindRuleEvaluationError(f"rule {rule.rule_id!r} has no conditions")
    logic = rule.condition_logic
    if logic is EnumContextTaskKindConditionLogic.ALL:
        matched = all(outcome.matched for outcome in outcomes)
    elif logic is EnumContextTaskKindConditionLogic.ANY:
        matched = any(outcome.matched for outcome in outcomes)
    else:
        raise TaskKindRuleEvaluationError(
            f"rule {rule.rule_id!r} has unknown condition_logic {logic!r}"
        )
    return ModelContextTaskKindRuleOutcome(
        rule_id=rule.rule_id,
        task_kind=rule.task_kind,
        priority=rule.priority,
        matched=matched,
        factors=rule.factors,
        conditions=outcomes,
    )


class HandlerContextTaskKindRules:
    """Evaluates every task-kind rule of one profile; the same request gives the same result."""

    def handle(
        self, request: ModelContextTaskKindRulesRequest
    ) -> ModelContextTaskKindRulesResult:
        profile = request.profile
        try:
            outcomes = [
                _evaluate_rule(rule, request.caller_scope)
                for rule in profile.task_kind_rules
            ]
        except TaskKindRuleEvaluationError as exc:
            return ModelContextTaskKindRulesResult(
                status=EnumContextTaskKindRulesStatus.ERROR,
                profile_name=request.profile_name,
                request_id=request.request_id,
                error=str(exc),
            )
        outcomes.sort(key=lambda outcome: (outcome.priority.rank, outcome.rule_id))
        matched = tuple(outcome for outcome in outcomes if outcome.matched)
        unmatched = tuple(outcome for outcome in outcomes if not outcome.matched)

        admitted: set[EnumContextFactor] = set(profile.unconditional_factors())
        admitted.update(factor for outcome in matched for factor in outcome.factors)
        declared = profile.required_factors + profile.optional_factors
        ordered = [f for f in profile.factor_precedence if f in admitted]
        ordered += [f for f in declared if f in admitted and f not in ordered]
        withheld = tuple(
            f
            for f in profile.factor_precedence + declared
            if f in profile.conditional_factors() and f not in admitted
        )
        return ModelContextTaskKindRulesResult(
            status=EnumContextTaskKindRulesStatus.OK,
            profile_name=request.profile_name,
            task_kind=matched[0].task_kind if matched else TASK_KIND_UNKNOWN,
            matched_rules=matched,
            unmatched_rules=unmatched,
            admitted_factors=tuple(ordered),
            withheld_factors=tuple(dict.fromkeys(withheld)),
            request_id=request.request_id,
        )


__all__ = ["HandlerContextTaskKindRules", "TaskKindRuleEvaluationError"]
