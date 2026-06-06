# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request models for node_on_vs_off_experiment_compute (OMN-12661).

ON path: inference with OmniNode context pack injected into system prompt.
OFF path: inference with baseline system prompt only (no context pack).

Both paths use the same task set and pricing table to produce a comparable
cost-delta evidence bundle. This node is pure and offline-capable; it accepts
pre-captured token counts via fixture mode (no live .201 dependency).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelOnVsOffTask(BaseModel):
    """A single task in the fixed task set for the ON/OFF experiment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(description="Stable identifier for this task, e.g. 'task_001'")
    description: str = Field(description="Human-readable description of the task")
    # Pre-captured token counts for replay-proven mode (fixture data).
    # When None the handler operates in runtime-observed mode and requires
    # a live LLM effect handler.
    on_prompt_tokens: int | None = Field(
        default=None,
        description="Pre-captured ON-path prompt tokens (fixture/replay mode)",
    )
    on_completion_tokens: int | None = Field(
        default=None,
        description="Pre-captured ON-path completion tokens (fixture/replay mode)",
    )
    off_prompt_tokens: int | None = Field(
        default=None,
        description="Pre-captured OFF-path prompt tokens (fixture/replay mode)",
    )
    off_completion_tokens: int | None = Field(
        default=None,
        description="Pre-captured OFF-path completion tokens (fixture/replay mode)",
    )


class ModelOnVsOffPricing(BaseModel):
    """Per-model pricing config in USD per 1k tokens."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_cost_per_1k: float = Field(
        ge=0.0, description="Cost per 1k prompt tokens in USD"
    )
    completion_cost_per_1k: float = Field(
        ge=0.0, description="Cost per 1k completion tokens in USD"
    )


class ModelOnVsOffRequest(BaseModel):
    """Input to the ON-vs-OFF experiment compute node.

    fixture_mode=True means all tasks carry pre-captured token counts and
    proof_class will be replay-proven. fixture_mode=False requires a live
    effect handler and yields runtime-observed-only proof.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(description="Stable run identifier, e.g. 'omn-12661-run-001'")
    model_id: str = Field(description="Model identifier used for both ON and OFF paths")
    tasks: tuple[ModelOnVsOffTask, ...] = Field(min_length=1)
    pricing: ModelOnVsOffPricing
    fixture_mode: bool = Field(
        default=True,
        description=(
            "True = all token counts are caller-supplied (replay-proven). "
            "False = live inference required (runtime-observed-only)."
        ),
    )


__all__ = [
    "ModelOnVsOffPricing",
    "ModelOnVsOffRequest",
    "ModelOnVsOffTask",
]
