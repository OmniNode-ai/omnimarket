# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A text block of a native prompt, with an optional cache breakpoint (OMN-20910).

The native Messages API caches a PREFIX of tools, then system, then messages:
a breakpoint on a block caches everything up to and including it. A single
prompt string cannot carry a breakpoint between a large stable part (the diff)
and a small varying part (the persona focus), so a native call is described as
ordered blocks instead.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelPromptTextBlock(BaseModel):
    """One text block; ``cache_ttl`` set puts a cache breakpoint after it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(min_length=1)
    cache_ttl: Literal["5m", "1h"] | None = Field(
        default=None,
        description="Cache the prefix up to this block for this long; none = no breakpoint.",
    )


class ModelPromptMessage(BaseModel):
    """One conversation turn made of ordered text blocks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["user", "assistant"]
    blocks: tuple[ModelPromptTextBlock, ...] = Field(min_length=1)


__all__ = ["ModelPromptMessage", "ModelPromptTextBlock"]
