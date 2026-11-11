# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the code-engine step order and attempts per engine from the routing contract and overlay.

The handler reads three yaml files (task_class_contracts, routing_tiers, and
the overlay), validates the overlay, determines the tier order for the task
class, checks the lab-first rule, and emits the ordered steps with their
attempts and enabled status. All engine names and tier mappings come from the
overlay; the handler contains no constant engine names, tier names, or numbers
other than the enum itself.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_delegation_code_step_order_effect.models.model_code_step_order import (
    EnumCodeStepRefusal,
    ModelCodeStep,
    ModelCodeStepOrderOverlay,
    ModelCodeStepOrderRequest,
    ModelCodeStepOrderResult,
)


def _load_yaml(path: str) -> dict[str, object] | None:
    """Load a yaml file, returning None on any error."""
    try:
        data = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict):
        return None
    return data


class HandlerCodeStepOrder:
    """Resolve the code step order for a task class."""

    def handle(self, request: ModelCodeStepOrderRequest) -> ModelCodeStepOrderResult:
        contracts = _load_yaml(request.task_class_contracts_path)
        if contracts is None:
            return ModelCodeStepOrderResult(
                ok=False,
                refusal=EnumCodeStepRefusal.CONTRACT_UNREADABLE,
                detail=f"cannot read {request.task_class_contracts_path}",
            )

        tiers = _load_yaml(request.routing_tiers_path)
        if tiers is None:
            return ModelCodeStepOrderResult(
                ok=False,
                refusal=EnumCodeStepRefusal.CONTRACT_UNREADABLE,
                detail=f"cannot read {request.routing_tiers_path}",
            )

        overlay_raw = _load_yaml(request.overlay_path)
        if overlay_raw is None:
            return ModelCodeStepOrderResult(
                ok=False,
                refusal=EnumCodeStepRefusal.OVERLAY_UNREADABLE,
                detail=f"cannot read {request.overlay_path}",
            )

        try:
            overlay = ModelCodeStepOrderOverlay.model_validate(overlay_raw)
        except ValidationError as exc:
            return ModelCodeStepOrderResult(
                ok=False,
                refusal=EnumCodeStepRefusal.OVERLAY_INVALID,
                detail=f"overlay validation failed: {exc.error_count()} error(s)",
            )

        task_classes = contracts.get("task_classes")
        if not isinstance(task_classes, dict) or request.task_class not in task_classes:
            return ModelCodeStepOrderResult(
                ok=False,
                refusal=EnumCodeStepRefusal.TASK_CLASS_UNKNOWN,
                detail=f"task class {request.task_class!r} not found in contracts",
            )

        class_entry = task_classes[request.task_class]
        if not isinstance(class_entry, dict):
            return ModelCodeStepOrderResult(
                ok=False,
                refusal=EnumCodeStepRefusal.TASK_CLASS_UNKNOWN,
                detail=f"task class {request.task_class!r} entry is not a mapping",
            )

        escalation = class_entry.get("escalation_policy")
        if not isinstance(escalation, dict):
            escalation = {}
        tier_order = escalation.get("tier_order")

        tier_list = tiers.get("tiers")
        if not isinstance(tier_list, list):
            tier_list = []
        tier_names = [
            t["name"]
            for t in tier_list
            if isinstance(t, dict) and isinstance(t.get("name"), str)
        ]
        tier_by_name = {
            t["name"]: t
            for t in tier_list
            if isinstance(t, dict) and isinstance(t.get("name"), str)
        }

        if not isinstance(tier_order, list) or not tier_order:
            tier_order = tier_names

        for tier_name in tier_order:
            if tier_name not in tier_by_name:
                return ModelCodeStepOrderResult(
                    ok=False,
                    refusal=EnumCodeStepRefusal.TIER_UNKNOWN,
                    detail=f"tier {tier_name!r} not found in routing_tiers",
                )

        lab_first = contracts.get("lab_first_policy")
        lab_tier = lab_first.get("lab_tier") if isinstance(lab_first, dict) else None
        if not isinstance(lab_tier, str):
            return ModelCodeStepOrderResult(
                ok=False,
                refusal=EnumCodeStepRefusal.CONTRACT_UNREADABLE,
                detail="lab_first_policy.lab_tier is absent from the contract",
            )
        if tier_order[0] != lab_tier:
            return ModelCodeStepOrderResult(
                ok=False,
                refusal=EnumCodeStepRefusal.NOT_LAB_FIRST,
                detail=f"first tier {tier_order[0]!r} is not the lab tier {lab_tier!r}",
            )

        disabled = overlay.disabled_engines_by_task_class.get(request.task_class, ())
        steps: list[ModelCodeStep] = []
        for tier_name in tier_order:
            tier_data = tier_by_name[tier_name]
            attempts = tier_data.get("max_retries", 0)
            if not isinstance(attempts, int) or attempts < 0:
                attempts = 0
            engines = overlay.engines_by_tier.get(tier_name, ())
            for engine in engines:
                steps.append(
                    ModelCodeStep(
                        tier=tier_name,
                        engine=engine,
                        attempts=attempts,
                        enabled=engine not in disabled,
                    )
                )

        if not any(s.enabled for s in steps):
            return ModelCodeStepOrderResult(
                ok=False,
                refusal=EnumCodeStepRefusal.NO_ENABLED_STEP,
                detail="no enabled step for this task class",
            )

        return ModelCodeStepOrderResult(
            ok=True,
            steps=tuple(steps),
            refusal=None,
            detail="",
        )
