# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract phases and required overlay budgets for work progress compute.

Budgets come from ``<root>/node_work_progress_compute/overlay.yaml`` under
``ONEX_SKILL_OVERLAY_ROOTS``. No default budget is packaged.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib.resources import files

import yaml

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.model_work_progress import (
    ModelWorkProgressBudgets,
    ModelWorkProgressPhase,
)

OVERLAY_ID = NODE_NAME = "node_work_progress_compute"
_PACKAGE = "omnimarket.nodes.node_work_progress_compute"


def load_phases() -> tuple[ModelWorkProgressPhase, ...]:
    """Read the ordered phase vocabulary from the packaged contract."""
    raw = yaml.safe_load(files(_PACKAGE).joinpath("contract.yaml").read_text("utf-8"))
    if not isinstance(raw, dict):
        raise NodeOverlayError(f"{NODE_NAME} contract.yaml is not a mapping")
    return tuple(
        ModelWorkProgressPhase.model_validate(phase)
        for phase in raw["config"]["work_progress"]["phases"]
    )


def load_phase_notes() -> dict[str, str]:
    """Read the contract's explanation for each phase."""
    return {phase.name: phase.note for phase in load_phases()}


def load_budgets(environ: Mapping[str, str] | None = None) -> dict[str, int]:
    """Require exactly one positive overlay budget for every contract phase."""
    overlay = load_structured_overlay(NODE_NAME, ModelWorkProgressBudgets, environ)
    phases = {phase.name for phase in load_phases()}
    supplied = set(overlay.budget_seconds)
    missing = sorted(phases - supplied)
    unknown = sorted(supplied - phases)
    if missing or unknown:
        problems = [
            f"missing {NODE_NAME}.budget_seconds.{phase}" for phase in missing
        ] + [f"unknown {NODE_NAME}.budget_seconds.{phase}" for phase in unknown]
        raise NodeOverlayError("; ".join(problems) + "; check ONEX_SKILL_OVERLAY_ROOTS")
    return dict(overlay.budget_seconds)


def load_episode_phases() -> dict[str, tuple[str, ...]]:
    """Read the phase vocabulary of each watched kind from the packaged contract."""
    raw = yaml.safe_load(files(_PACKAGE).joinpath("contract.yaml").read_text("utf-8"))
    if not isinstance(raw, dict):
        raise NodeOverlayError(f"{NODE_NAME} contract.yaml is not a mapping")
    kinds = raw["config"]["work_episodes"]["kinds"]
    return {str(kind): tuple(str(p) for p in phases) for kind, phases in kinds.items()}


def load_episode_budgets(
    environ: Mapping[str, str] | None = None,
) -> dict[str, dict[str, int]]:
    """Require exactly one positive overlay limit for every phase of every watched kind."""
    overlay = load_structured_overlay(NODE_NAME, ModelWorkProgressBudgets, environ)
    supplied = overlay.episode_limit_seconds
    if supplied is None:
        raise NodeOverlayError(
            f"missing {NODE_NAME}.episode_limit_seconds; check ONEX_SKILL_OVERLAY_ROOTS"
        )
    declared = load_episode_phases()
    problems: list[str] = []
    for kind in sorted(set(declared) | set(supplied)):
        if kind not in supplied:
            problems.append(f"missing {NODE_NAME}.episode_limit_seconds.{kind}")
            continue
        if kind not in declared:
            problems.append(f"unknown {NODE_NAME}.episode_limit_seconds.{kind}")
            continue
        problems.extend(
            f"missing {NODE_NAME}.episode_limit_seconds.{kind}.{phase}"
            for phase in declared[kind]
            if phase not in supplied[kind]
        )
        problems.extend(
            f"unknown {NODE_NAME}.episode_limit_seconds.{kind}.{phase}"
            for phase in supplied[kind]
            if phase not in declared[kind]
        )
    if problems:
        raise NodeOverlayError("; ".join(problems) + "; check ONEX_SKILL_OVERLAY_ROOTS")
    return {kind: dict(phases) for kind, phases in supplied.items()}


def load_round_job_budgets(environ: Mapping[str, str] | None = None) -> dict[str, int]:
    """The optional per-job override of the scheduled round limit (empty when the overlay has none)."""
    overlay = load_structured_overlay(NODE_NAME, ModelWorkProgressBudgets, environ)
    return dict(overlay.round_job_limit_seconds or {})


__all__ = [
    "NODE_NAME",
    "OVERLAY_ID",
    "load_budgets",
    "load_episode_budgets",
    "load_episode_phases",
    "load_phase_notes",
    "load_phases",
    "load_round_job_budgets",
]
