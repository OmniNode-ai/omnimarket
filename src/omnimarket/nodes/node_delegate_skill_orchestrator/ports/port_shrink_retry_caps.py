# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the rubric-failure ladder's caps from the task-class contract (OMN-20172)."""

from __future__ import annotations

from omnimarket.models.delegation.model_shrink_retry import ModelShrinkRetryCaps
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    _get_task_class_contract,
    _task_class_entry,
)

_CAP_KEYS = ("max_config_swaps", "max_split_depth", "unit_attempts_per_unit")


def resolve_task_class_shrink_caps(task_type: str) -> ModelShrinkRetryCaps | None:
    """Resolve the rubric-failure ladder's caps for ``task_type`` (OMN-20172).

    Read from ``escalation_policy`` in the same contract both delegation paths
    route by. None when the class declares no ``max_config_swaps``,
    ``max_split_depth`` and ``unit_attempts_per_unit`` (as integers) and a
    ``shrink_unit_prompt`` holding ``{source}``, so a class that declares no caps
    neither swaps nor shrinks and goes straight to its next responder: the caps
    are a property of the contract and the policy holds none of its own.
    """
    entry = _task_class_entry(_get_task_class_contract(), task_type)
    if entry is None:
        return None
    escalation = entry.get("escalation_policy")
    if not isinstance(escalation, dict):
        return None
    ints = {key: escalation.get(key) for key in _CAP_KEYS}
    unit_prompt = escalation.get("shrink_unit_prompt")
    if any(not isinstance(v, int) or isinstance(v, bool) for v in ints.values()):
        return None
    if not isinstance(unit_prompt, str) or "{source}" not in unit_prompt:
        return None
    return ModelShrinkRetryCaps.model_validate({**ints, "unit_prompt": unit_prompt})
