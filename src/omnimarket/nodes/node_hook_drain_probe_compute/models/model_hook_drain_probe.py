# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result of the hook-emit drain probe (OMN-19998)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelHookDrainProbeRequest(BaseModel):
    """The event class of every pending journal record, head first, and what the process already proved refused."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pending_event_types: tuple[str, ...] = Field(
        description="The event class of each pending record, in journal order; index 0 is the refused head"
    )
    refused_event_types: tuple[str, ...] = Field(
        default=(),
        description="Classes this process already proved refused: asking one measures the class, not the broker",
    )
    max_classes: int = Field(
        default=3,
        ge=1,
        le=16,
        description="How many different classes the probe asks before it reads the broker as unreachable",
    )


class ModelHookDrainProbeResult(BaseModel):
    """The indexes to publish, in order, until one is accepted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_order: tuple[int, ...] = Field(
        description="Indexes into pending_event_types, one per class; empty when no record can serve as a probe"
    )
    reason: str = Field(
        description="Why this order: different-classes, directly-behind or fewer-than-two-pending"
    )
