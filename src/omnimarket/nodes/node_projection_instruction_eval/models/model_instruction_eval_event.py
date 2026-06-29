# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Typed input event for instruction-eval aggregate projection snapshots."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelInstructionEvalProjectionEvent(BaseModel):
    """Instruction-eval result event consumed by the instruction-eval aggregate reducer.

    Emitted by the instruction-eval runner / scorer
    (onex-self-extending-agent/eval/instruction-eval lineage) as
    onex.evt.omnimarket.instruction-eval-result.v1.

    pass_rate is Optional because a run may not produce a pass/fail decision
    (e.g. scorer error). None is stored as NULL in the projection table; the
    dashboard panel renders an em-dash for absent cells rather than a fake 0%.
    """

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    model: str = Field(description="Model identifier (e.g. 'ds4-flash', 'qwen-27b')")
    task: str = Field(
        description="Instruction task slug (e.g. 'python-version', 'no-hardcoded-paths')"
    )
    context_mode: str = Field(
        description="Context mode: 'baseline', 'chunk', or 'full-claude-md'"
    )
    pass_rate: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Mean pass rate 0-1 across runs; None when absent",
    )
    output_tokens: int = Field(
        default=0,
        ge=0,
        description="Mean output tokens across runs",
    )
    runs: int = Field(
        default=0,
        ge=0,
        description="Number of eval runs aggregated",
    )


__all__ = ["ModelInstructionEvalProjectionEvent"]
