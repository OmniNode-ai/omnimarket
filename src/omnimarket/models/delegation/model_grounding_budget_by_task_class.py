# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-task-class grounded input budgets for delegation backends (OMN-20486)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelGroundingBudgetByTaskClass(BaseModel):
    """Per-task-class overrides of a backend's ``max_grounded_input_tokens``.

    The backend default was set for an identifier-grounding failure on a
    summary task and was not measured for a code-edit loop turn, which re-reads
    the file it edits. A class named here for a backend uses its own budget;
    every other class keeps the backend default from the bifrost contract.

    This is its own packaged config, not a field on the bifrost wire model, so
    a released consumer that decodes the bifrost contract never sees the key
    (the Wire Compatibility Gate, OMN-18868).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    backends: dict[str, dict[str, int]] = Field(
        default_factory=dict,
        description=(
            "Backend id to (task class to grounded input token budget). Each "
            "value must cite its measured readback in the comment above it."
        ),
    )

    def budget_for(self, backend_id: str, task_type: str) -> int | None:
        """Return the class budget for ``backend_id``, or ``None`` when unset."""
        return self.backends.get(backend_id, {}).get(task_type)


__all__ = ["ModelGroundingBudgetByTaskClass"]
