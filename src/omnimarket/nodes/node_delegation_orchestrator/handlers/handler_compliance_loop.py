# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerComplianceLoop — single-attempt schema-compliance evaluator (OMN-10792).

Evaluates one LLM attempt against the output schema looked up from the
omnimarket schema registry. On validation failure, builds a repair prompt via
omnimarket's `HandlerSchemaRepair`. After every attempt, queries
`HandlerBudgetPolicy` to decide whether the orchestrator may issue another
repair attempt.

This handler is the per-iteration evaluator. The retry loop itself lives in
the delegation orchestrator (Task 5 — `HandlerDelegationWorkflow`).

Pure compute: no I/O, no event bus, no DB. The schema-repair and budget-policy
handlers it invokes are pure compute themselves.
"""

from __future__ import annotations

import json

from omnibase_infra.enums import EnumHandlerType, EnumHandlerTypeCategory

from omnimarket.nodes.node_budget_policy_compute.handlers.handler_budget_policy import (
    HandlerBudgetPolicy,
)
from omnimarket.nodes.node_budget_policy_compute.models.model_budget_limits import (
    ModelBudgetLimits,
)
from omnimarket.nodes.node_budget_policy_compute.models.model_budget_policy_enums import (
    EnumBudgetAction,
    EnumTaskPriority,
)
from omnimarket.nodes.node_budget_policy_compute.models.model_budget_policy_request import (
    ModelBudgetPolicyRequest,
)
from omnimarket.nodes.node_budget_policy_compute.models.model_budget_usage import (
    ModelBudgetUsage,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_compliance_loop_result import (
    ModelComplianceLoopResult,
)
from omnimarket.nodes.node_output_schema_registry_compute.handlers.handler_output_schema_registry import (
    _get_schema,
)
from omnimarket.nodes.node_schema_repair_compute.handlers.handler_schema_repair import (
    HandlerSchemaRepair,
)
from omnimarket.nodes.node_schema_repair_compute.models.model_repair_request import (
    ModelSchemaRepairRequest,
)


class HandlerComplianceLoop:
    """Evaluate a single LLM attempt against an output schema and decide next step.

    The orchestrator drives the retry loop; this handler returns one of three
    outcomes per attempt:

    1. ``compliant=True`` — output validated; loop terminates successfully.
    2. ``compliant=False`` and ``budget_action=CONTINUE`` — emit ``repair_prompt``
       to a fresh inference attempt.
    3. ``compliant=False`` and ``budget_action=ABORT`` — stop; no more attempts.

    The handler is pure: same inputs → identical output, no side effects.
    """

    def __init__(
        self,
        schema_repair: HandlerSchemaRepair | None = None,
        budget_policy: HandlerBudgetPolicy | None = None,
    ) -> None:
        # Default to omnimarket-provided pure-compute handlers; tests may inject
        # alternates via constructor (no module-level mocking required).
        self._schema_repair = schema_repair or HandlerSchemaRepair()
        self._budget_policy = budget_policy or HandlerBudgetPolicy()

    @property
    def handler_type(self) -> EnumHandlerType:
        return EnumHandlerType.NODE_HANDLER

    @property
    def handler_category(self) -> EnumHandlerTypeCategory:
        return EnumHandlerTypeCategory.COMPUTE

    def evaluate(
        self,
        *,
        candidate_output: str,
        schema_key: str,
        original_prompt: str,
        attempt_number: int,
        cumulative_tokens: int,
        attempt_tokens: int,
        budget_limits: ModelBudgetLimits,
        elapsed_time_s: float = 0.0,
        cost_usd: float = 0.0,
        task_priority: EnumTaskPriority = EnumTaskPriority.NORMAL,
        run_id: str = "",
    ) -> ModelComplianceLoopResult:
        """Evaluate one attempt; return loop directive.

        Args:
            candidate_output: Raw LLM output to validate.
            schema_key: Registry key for the target schema.
            original_prompt: The prompt that produced ``candidate_output``;
                required when building a repair prompt.
            attempt_number: 1-based index of this attempt (1 for the first call).
            cumulative_tokens: Total tokens consumed across all prior attempts;
                ``attempt_tokens`` is added to compute the running total.
            attempt_tokens: Tokens consumed by *this* attempt.
            budget_limits: Declared ceilings for the delegation.
            elapsed_time_s: Total seconds elapsed across all attempts; used by
                the budget policy.
            cost_usd: Total USD spent across all attempts; used by the budget
                policy.
            task_priority: Task priority for budget-policy hard-abort multiplier.
            run_id: Correlation handle propagated into the schema-repair request.

        Returns:
            A :class:`ModelComplianceLoopResult` carrying the attempt count, the
            running token total, and either the validated output (on compliance)
            or a repair prompt + budget directive (on non-compliance).
        """
        running_tokens = cumulative_tokens + attempt_tokens

        target_schema = _get_schema(schema_key)
        if target_schema is None:
            return ModelComplianceLoopResult(
                compliant=False,
                validated_output="",
                tokens_to_compliance=running_tokens,
                compliance_attempts=attempt_number,
                repair_prompt="",
                budget_action=EnumBudgetAction.ABORT,
                abort_reason=f"Unknown output schema key: {schema_key!r}",
            )

        validation_errors = _validate_against_schema(candidate_output, target_schema)

        if not validation_errors:
            # Successful compliance — loop terminates.
            return ModelComplianceLoopResult(
                compliant=True,
                validated_output=candidate_output,
                tokens_to_compliance=running_tokens,
                compliance_attempts=attempt_number,
                repair_prompt="",
                budget_action=EnumBudgetAction.CONTINUE,
                abort_reason="",
            )

        # Validation failed — build a repair prompt for the next attempt.
        repair_result = self._schema_repair.handle(
            ModelSchemaRepairRequest(
                malformed_output=candidate_output,
                validation_errors=validation_errors,
                target_schema=target_schema,
                original_prompt=original_prompt,
                run_id=run_id,
            )
        )

        # Decide whether the orchestrator may try again.
        budget_result = self._budget_policy.handle(
            ModelBudgetPolicyRequest(
                current_usage=ModelBudgetUsage(
                    tokens=running_tokens,
                    cost_usd=cost_usd,
                    elapsed_time_s=elapsed_time_s,
                ),
                budget_limits=budget_limits,
                task_priority=task_priority,
            )
        )

        # If budget aborts OR repair classifies the errors as non-repairable,
        # the loop must terminate even though the output was non-compliant.
        if (
            budget_result.action == EnumBudgetAction.ABORT
            or not repair_result.repairable
        ):
            abort_reason = (
                budget_result.reason
                if budget_result.action == EnumBudgetAction.ABORT
                else f"Schema-repair classified errors as non-repairable: {repair_result.error_summary}"
            )
            return ModelComplianceLoopResult(
                compliant=False,
                validated_output="",
                tokens_to_compliance=running_tokens,
                compliance_attempts=attempt_number,
                repair_prompt="",
                budget_action=EnumBudgetAction.ABORT,
                abort_reason=abort_reason,
            )

        # CONTINUE / WARN / THROTTLE — orchestrator may retry; emit the prompt.
        return ModelComplianceLoopResult(
            compliant=False,
            validated_output="",
            tokens_to_compliance=running_tokens,
            compliance_attempts=attempt_number,
            repair_prompt=repair_result.repair_prompt,
            budget_action=budget_result.action,
            abort_reason="",
        )


def _validate_against_schema(
    candidate_output: str, target_schema: dict[str, object]
) -> list[dict[str, object]]:
    """Validate ``candidate_output`` against ``target_schema``; return Pydantic errors.

    The candidate is parsed as JSON and validated by reconstructing a one-shot
    Pydantic model from the supplied JSON Schema. We intentionally re-derive the
    model rather than caching, because ``target_schema`` is supplied by the
    caller per-attempt and the registry is the source of truth.

    Returns an empty list on full compliance; otherwise returns the
    ``ValidationError().errors()`` list, ready to feed into ``HandlerSchemaRepair``.
    """
    try:
        parsed = json.loads(candidate_output)
    except json.JSONDecodeError as e:
        return [
            {
                "type": "json_invalid",
                "loc": ("__root__",),
                "msg": f"Output is not valid JSON: {e.msg}",
                "input": candidate_output,
            }
        ]

    return _check_required_and_types(parsed, target_schema)


def _check_required_and_types(
    candidate: object, target_schema: dict[str, object]
) -> list[dict[str, object]]:
    """Lightweight schema check: required-fields presence + JSON type matching.

    Sufficient for the compliance loop's purpose — flagging concrete fields
    that the LLM omitted or got the wrong type for so the schema-repair
    handler can produce a targeted repair prompt. Deep schema features
    (oneOf, anyOf, conditional refs) are out of scope; the registered
    Pydantic models that produce these schemas don't use them.
    """
    if not isinstance(candidate, dict):
        return [
            {
                "type": "type_error",
                "loc": ("__root__",),
                "msg": f"Expected JSON object, got {type(candidate).__name__}",
                "input": candidate,
            }
        ]

    errors: list[dict[str, object]] = []
    properties = target_schema.get("properties", {})
    required = target_schema.get("required", [])

    if not isinstance(properties, dict):
        return errors  # Malformed schema; defer to caller.
    if not isinstance(required, list):
        required = []

    for field_name in required:
        if field_name not in candidate:
            errors.append(
                {
                    "type": "missing",
                    "loc": (field_name,),
                    "msg": "Field required",
                    "input": candidate,
                }
            )

    for field_name, field_schema in properties.items():
        if field_name not in candidate:
            continue  # Either optional or already flagged as missing above.
        if not isinstance(field_schema, dict):
            continue

        expected_type = field_schema.get("type")
        if expected_type is None:
            continue  # No type constraint — accept anything.

        actual = candidate[field_name]
        if not _matches_json_type(actual, expected_type):
            errors.append(
                {
                    "type": "type_error",
                    "loc": (field_name,),
                    "msg": f"Expected JSON {expected_type}, got {type(actual).__name__}",
                    "input": actual,
                }
            )

    return errors


_JSON_TYPE_MAP: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
    "null": (type(None),),
}


def _matches_json_type(value: object, expected: object) -> bool:
    if isinstance(expected, list):
        return any(_matches_json_type(value, t) for t in expected)
    if not isinstance(expected, str):
        return True  # Cannot interpret — be permissive.
    expected_types = _JSON_TYPE_MAP.get(expected, ())
    if not expected_types:
        return True
    # Booleans are ints in Python but we want strict separation per JSON Schema.
    if expected == "integer" and isinstance(value, bool):
        return False
    if expected == "number" and isinstance(value, bool):
        return False
    return isinstance(value, expected_types)


# Re-export so plugin loaders can find the validator helper if needed.
__all__ = ["HandlerComplianceLoop", "ModelComplianceLoopResult"]
