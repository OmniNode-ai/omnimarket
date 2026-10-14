# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Required-bar authority for delegation score-vs-bar escalation."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from omnimarket.inference.task_class_authority import (
    resolve_task_class_execution_required,
)
from omnimarket.models.delegation.wire.model_quality_gate import (
    NO_NAMED_TEST_RUN_REASON,
    SCORE_VERIFYING_CHECKS,
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    _get_task_class_contract,
    _task_class_entry,
)


class RequiredBarAuthorityError(ValueError):
    """Raised when required-bar authority is missing or invalid."""


@dataclass(frozen=True)
class RequiredBarAuthority:
    """Resolved required-bar authority for a delegation quality decision."""

    required_bar: float
    authority_source: str
    score_source: str
    request_override_applied: bool = False
    override_within_bounds: bool = True
    # OMN-21077: a code class (``execution_required: true``) is accepted on its
    # deterministic checks and an executed test run. Its ``required_bar`` is
    # still resolved and reported, but it decides nothing: the graded score
    # carries keyword heuristics, and a keyword cannot tell working code from
    # a stub.
    acceptance_by_executed_tests: bool = False


def meets_required_bar(result: ModelQualityGateResult, required_bar: float) -> bool:
    """Apply the score bar for a class whose answer is not executed.

    Deterministic acceptance gets no exemption from the bar (OMN-21077).
    """
    return (
        result.fail_category != "fail_deterministic"
        and result.passed
        and result.quality_score >= required_bar
    )


def passes_executed_checks(
    result: ModelQualityGateResult, declared_deterministic: Iterable[str]
) -> bool:
    """Whether a code answer passed every deterministic check and its tests ran.

    OMN-21077: every declared deterministic check passed, and each declared
    score-verifying check (``passes_existing_tests``) has an executed, passing
    evaluation on the result. A verifying check that was skipped, or a result
    from a gate that never evaluated it, is not a pass. The score is not read.
    """
    if result.fail_category == "fail_deterministic" or not result.passed:
        return False
    executed_passes = {
        evaluation.rule for evaluation in result.rule_evaluations if evaluation.passed
    }
    return all(
        check in executed_passes
        for check in declared_deterministic
        if check in SCORE_VERIFYING_CHECKS
    )


def with_executed_check_verdict(
    result: ModelQualityGateResult,
    authority: RequiredBarAuthority,
    *,
    declared_deterministic: Iterable[str],
) -> ModelQualityGateResult:
    """Refuse a code answer the gate passed with no executed test run (OMN-21077).

    A gate that predates the INSUFFICIENT rule, or a check set that lost
    ``passes_existing_tests`` on the way, can pass a code answer nobody ran.
    Both paths apply this before deciding, so the refusal carries the same
    reason, category and futility as the gate's own, and a failed terminal
    names the criterion that failed. Every other result is returned unchanged.
    """
    if (
        not authority.acceptance_by_executed_tests
        or not result.passed
        or passes_executed_checks(result, declared_deterministic)
    ):
        return result
    return result.model_copy(
        update={
            "passed": False,
            "fail_category": "fail_deterministic",
            "failure_reasons": (*result.failure_reasons, NO_NAMED_TEST_RUN_REASON),
            "fallback_recommended": True,
        }
    )


def is_quality_accepted(
    result: ModelQualityGateResult,
    authority: RequiredBarAuthority,
    *,
    declared_deterministic: Iterable[str],
) -> bool:
    """The one acceptance verdict the bus orchestrator and bus-less port share.

    A code class is decided by its executed checks; every other class by its
    declared bar (OMN-21077).
    """
    if authority.acceptance_by_executed_tests:
        return passes_executed_checks(result, declared_deterministic)
    return meets_required_bar(result, authority.required_bar)


def resolve_required_bar_authority(
    *,
    task_type: str,
    workflow_name: str | None = None,
    request_override: float | None = None,
) -> RequiredBarAuthority:
    """Resolve the score threshold from task-class/workflow/request authority."""
    contract = _get_task_class_contract()
    entry = _task_class_entry(contract, task_type)
    if entry is None:
        raise RequiredBarAuthorityError(
            f"required_bar missing: task_class {task_type!r} is not declared"
        )

    policy = _quality_gate_policy(entry)
    if policy is None or "required_bar" not in policy:
        raise RequiredBarAuthorityError(
            f"required_bar missing for task_class {task_type!r}"
        )

    required_bar = _bar_value(policy["required_bar"], "required_bar")
    min_bar, max_bar = _override_bounds(policy)
    score_source = _score_source(policy)
    authority_source = f"task_class:{task_type}"
    acceptance_by_executed_tests = resolve_task_class_execution_required(task_type)

    workflow_bar = _workflow_override(policy, workflow_name)
    if workflow_bar is not None:
        _assert_within_bounds(
            workflow_bar,
            min_bar=min_bar,
            max_bar=max_bar,
            source=f"workflow:{workflow_name}",
        )
        required_bar = workflow_bar
        authority_source = f"workflow:{workflow_name}"

    if request_override is not None:
        _assert_within_bounds(
            request_override,
            min_bar=min_bar,
            max_bar=max_bar,
            source="request_override",
        )
        required_bar = request_override
        authority_source = "request_override"
        return RequiredBarAuthority(
            required_bar=required_bar,
            authority_source=authority_source,
            score_source=score_source,
            request_override_applied=True,
            override_within_bounds=True,
            acceptance_by_executed_tests=acceptance_by_executed_tests,
        )

    return RequiredBarAuthority(
        required_bar=required_bar,
        authority_source=authority_source,
        score_source=score_source,
        acceptance_by_executed_tests=acceptance_by_executed_tests,
    )


def _quality_gate_policy(entry: dict[str, object]) -> dict[str, object] | None:
    policy = entry.get("quality_gate")
    if isinstance(policy, dict):
        return policy
    if "required_bar" in entry:
        return entry
    return None


def _score_source(policy: dict[str, object]) -> str:
    raw = policy.get("score_source")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return "quality_gate_graded_score"


def _workflow_override(
    policy: dict[str, object],
    workflow_name: str | None,
) -> float | None:
    if workflow_name is None:
        return None
    raw_overrides = policy.get("workflow_overrides")
    if not isinstance(raw_overrides, dict):
        return None
    raw = raw_overrides.get(workflow_name)
    if raw is None:
        return None
    if isinstance(raw, dict):
        raw = raw.get("required_bar")
    return _bar_value(raw, f"workflow_overrides.{workflow_name}.required_bar")


def _override_bounds(policy: dict[str, object]) -> tuple[float, float]:
    raw_bounds = policy.get("request_override_bounds")
    if not isinstance(raw_bounds, dict):
        raise RequiredBarAuthorityError(
            "request_override_bounds must be declared with min/max"
        )
    min_bar = _bar_value(raw_bounds.get("min"), "request_override_bounds.min")
    max_bar = _bar_value(raw_bounds.get("max"), "request_override_bounds.max")
    if min_bar > max_bar:
        raise RequiredBarAuthorityError(
            "request_override_bounds.min must be <= request_override_bounds.max"
        )
    return min_bar, max_bar


def _assert_within_bounds(
    value: float,
    *,
    min_bar: float,
    max_bar: float,
    source: str,
) -> None:
    if min_bar <= value <= max_bar:
        return
    raise RequiredBarAuthorityError(
        f"{source} required_bar {value:.3f} outside declared bounds "
        f"[{min_bar:.3f}, {max_bar:.3f}]"
    )


def _bar_value(raw: Any, field_name: str) -> float:
    if raw is None or isinstance(raw, bool):
        raise RequiredBarAuthorityError(f"{field_name} must be a number in [0, 1]")
    if isinstance(raw, int | float):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw)
        except ValueError as exc:
            raise RequiredBarAuthorityError(
                f"{field_name} must be a number in [0, 1]"
            ) from exc
    else:
        raise RequiredBarAuthorityError(f"{field_name} must be a number in [0, 1]")
    if not math.isfinite(value) or value < 0.0 or value > 1.0:
        raise RequiredBarAuthorityError(f"{field_name} must be a number in [0, 1]")
    return value


__all__ = [
    "RequiredBarAuthority",
    "RequiredBarAuthorityError",
    "is_quality_accepted",
    "meets_required_bar",
    "passes_executed_checks",
    "resolve_required_bar_authority",
    "with_executed_check_verdict",
]
