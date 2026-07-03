# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Required-bar authority for delegation score-vs-bar escalation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

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
        )

    return RequiredBarAuthority(
        required_bar=required_bar,
        authority_source=authority_source,
        score_source=score_source,
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
    "resolve_required_bar_authority",
]
