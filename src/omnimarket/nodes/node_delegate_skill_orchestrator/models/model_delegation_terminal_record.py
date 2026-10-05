# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decode the immutable terminal shared by replay and reaper handoff."""

from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
)


def terminal_from_record(
    record: dict[str, object],
) -> ModelDelegateSkillCompleted | ModelDelegateSkillFailed | None:
    """Rebuild a held terminal without inventing an unreadable result."""
    cls_name = record.get("cls")
    data = record.get("data")
    if not isinstance(data, dict):
        return None
    for candidate in (ModelDelegateSkillCompleted, ModelDelegateSkillFailed):
        if cls_name != candidate.__name__:
            continue
        try:
            return candidate.model_validate(data)
        except Exception:  # a stored row we cannot parse is not a terminal
            return None
    return None
