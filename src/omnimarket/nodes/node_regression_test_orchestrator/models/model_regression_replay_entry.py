# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One recorded entry of a regression replay corpus.

The SEA runner read a JSONL replay file of ``{task_id, attempt, output}`` rows
and keyed them ``(task_id, attempt)``. In the canonical node the corpus is part
of the typed command payload (no handler file I/O), so each row is a strongly
typed, frozen :class:`ModelRegressionReplayEntry`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelRegressionReplayEntry(BaseModel):
    """A recorded LLM response for one (task, attempt) of a regression replay."""

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    task_id: str = Field(
        ..., min_length=1, description="Task this response belongs to."
    )
    attempt: int = Field(..., ge=1, description="1-based attempt index for the task.")
    output: str = Field(
        ...,
        description=(
            "Recorded model output for the (task, attempt). Empty string means "
            "the recorded run produced no passing output (failed task)."
        ),
    )


__all__ = ["ModelRegressionReplayEntry"]
