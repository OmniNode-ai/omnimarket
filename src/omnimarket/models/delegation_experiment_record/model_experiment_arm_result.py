# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One arm of an experiment: its model, the runs it counts and its measured value."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelExperimentArmResult(BaseModel):
    """``run_ids`` are receipt run ids; the arm's run count is their number."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    arm_id: str
    model: str
    run_ids: tuple[str, ...] = ()
    per_arm_value: float | None = None
