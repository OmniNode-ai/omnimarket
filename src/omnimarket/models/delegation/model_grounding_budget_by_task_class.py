# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-task-class grounded input budgets and measured ceilings (OMN-20486, OMN-22220)."""

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

    measured_correct_ceilings: dict[str, dict[str, int]] = Field(
        default_factory=dict,
        description=(
            "Backend id to (task class to the largest input, in grounding "
            "tokens, the backend was measured to answer correctly). Each value "
            "must cite its measured run in the comment above it (OMN-22220)."
        ),
    )

    def ceiling_for(self, backend_id: str, task_type: str) -> int | None:
        """Return the measured correct ceiling, or ``None`` when not measured."""
        return self.measured_correct_ceilings.get(backend_id, {}).get(task_type)

    def budget_for(self, backend_id: str, task_type: str) -> int | None:
        """Return the class budget for ``backend_id``, or ``None`` when unset.

        OMN-22220: a measured correct ceiling bounds the budget, so a budget is
        never larger than the input the backend was measured to answer correctly.
        """
        declared = self.backends.get(backend_id, {}).get(task_type)
        ceiling = self.ceiling_for(backend_id, task_type)
        if ceiling is None:
            return declared
        return ceiling if declared is None else min(declared, ceiling)


__all__ = ["ModelGroundingBudgetByTaskClass"]
