# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A task class's delegation time limit, from the contract or a site overlay.

The task-class contract's ``execution_budgets`` entry is the base. A site may
override any field of a class's entry in ``<root>/delegation_budgets/overlay.yaml``
under the first root in ``ONEX_SKILL_OVERLAY_ROOTS`` that holds one. The merged
entry is validated as a whole, so the overlay is held to the contract's bounds.
The runtime port's wait and the handler's own bound follow the largest declared
class (:func:`largest_execution_budget`).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.handlers.node_overlay_reader import NodeOverlayError, find_node_overlay
from omnimarket.inference.task_class_authority import (
    ModelTaskClassAuthority,
    ModelTaskClassExecutionBudget,
    load_task_class_authority,
)
from omnimarket.models.delegation.model_execution_budget_overlay import (
    ModelExecutionBudgetOverlay,
    ModelResolvedExecutionBudget,
)

#: The overlay id a site uses to set a task class's execution budget.
EXECUTION_BUDGET_OVERLAY_ID = "delegation_budgets"
_CONTRACT_SOURCE = "contract:task_class_contracts.v1.yaml"

_LoadedOverlay = tuple[Path, ModelExecutionBudgetOverlay]


def load_execution_budget_overlay(
    authority: ModelTaskClassAuthority,
    environ: Mapping[str, str] | None = None,
) -> _LoadedOverlay | None:
    """Read the site's budget overlay, or ``None`` when no root holds one.

    A present overlay that is unreadable, malformed, or names a class the
    contract declares no budget for is refused: a broken overlay must not read
    as an absent one, and a typo must not leave a class at its contract value
    while the site believes it raised it.
    """
    path = find_node_overlay(EXECUTION_BUDGET_OVERLAY_ID, environ)
    if path is None:
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise NodeOverlayError(
            f"execution budget overlay {path} is unreadable ({type(exc).__name__})"
        ) from None
    try:
        overlay = ModelExecutionBudgetOverlay.model_validate(raw)
    except ValidationError as exc:
        raise NodeOverlayError(
            f"execution budget overlay {path} is invalid: {exc}"
        ) from None
    unknown = sorted(set(overlay.execution_budgets) - set(authority.execution_budgets))
    if unknown:
        raise NodeOverlayError(
            f"execution budget overlay {path} names task classes with no "
            f"contract budget: {unknown}"
        )
    return path, overlay


def _resolve(
    authority: ModelTaskClassAuthority,
    task_class: str,
    loaded: _LoadedOverlay | None,
) -> ModelResolvedExecutionBudget:
    base = authority.execution_budget(task_class)
    override = None if loaded is None else loaded[1].execution_budgets.get(task_class)
    if loaded is None or override is None:
        return ModelResolvedExecutionBudget(
            budget=base,
            source=f"{_CONTRACT_SOURCE}#execution_budgets.{task_class}",
        )
    merged = {**base.model_dump(), **override.model_dump(exclude_none=True)}
    try:
        budget = ModelTaskClassExecutionBudget.model_validate(merged)
    except ValidationError as exc:
        raise NodeOverlayError(
            f"execution budget overlay {loaded[0]} sets an invalid budget for "
            f"{task_class}: {exc}"
        ) from None
    return ModelResolvedExecutionBudget(
        budget=budget,
        source=f"overlay:{loaded[0]}#execution_budgets.{task_class}",
    )


def resolve_execution_budget(
    task_class: str,
    environ: Mapping[str, str] | None = None,
) -> ModelResolvedExecutionBudget:
    """Return ``task_class``'s budget, the overlay's when it sets one, and its source.

    Raises ``ValueError`` for a class with no contract budget, which refuses
    the dispatch, and :class:`NodeOverlayError` for a broken overlay.
    """
    authority = load_task_class_authority()
    loaded = load_execution_budget_overlay(authority, environ)
    return _resolve(authority, task_class, loaded)


def largest_execution_budget(
    environ: Mapping[str, str] | None = None,
) -> ModelResolvedExecutionBudget | None:
    """The declared class whose ceiling plus delivery margin is largest.

    The runtime port's wait and the handler's own bound follow this class, so
    raising one class in the overlay raises both with it. ``None`` when no
    class declares a budget.
    """
    authority = load_task_class_authority()
    loaded = load_execution_budget_overlay(authority, environ)
    resolved = [
        _resolve(authority, task_class, loaded)
        for task_class in sorted(authority.execution_budgets)
    ]
    if not resolved:
        return None
    return max(
        resolved,
        key=lambda item: (
            item.budget.task_class_timeout_ceiling_seconds
            + item.budget.terminal_delivery_margin_seconds
        ),
    )


__all__ = [
    "EXECUTION_BUDGET_OVERLAY_ID",
    "largest_execution_budget",
    "load_execution_budget_overlay",
    "resolve_execution_budget",
]
