# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one composed-runtime boot showed: /health probe answers and the event-loop holds a stack sampler recorded."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelHealthSample(BaseModel):
    """One /health probe: when it started after the container start, whether it was answered, and how long it took."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    offset_seconds: float = Field(
        ge=0.0, description="Probe start, seconds after the container start."
    )
    answered: bool = Field(
        description="True when the probe got an HTTP response with a body; a 503 counts."
    )
    latency_seconds: float = Field(
        ge=0.0, description="Seconds from probe start to its answer or its timeout."
    )


class ModelLoopHold(BaseModel):
    """One contiguous stretch in which a single event-loop callback held the loop, as a stack sampler saw it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hold_id: str = Field(
        min_length=1,
        description="Stable id of the callback invocation the sampler followed.",
    )
    held_seconds: float = Field(
        ge=0.0, description="Contiguous seconds the callback held the loop."
    )
    call: str = Field(
        min_length=1,
        description="The sampled call chain, outermost first: file:function:line frames joined by >.",
    )
    stack: tuple[str, ...] = Field(
        default=(),
        description="Frames of the sampled main-thread stack, outermost first.",
    )
    recorded: bool = Field(
        default=True,
        description="False when the figure comes from a measured number with no recorded log behind it.",
    )


class ModelBootObservation(BaseModel):
    """The probe answers and the holds of one boot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    health_samples: tuple[ModelHealthSample, ...] = ()
    loop_holds: tuple[ModelLoopHold, ...] = ()
