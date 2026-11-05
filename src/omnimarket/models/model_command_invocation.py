# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A command address and its invocation policy are separate contract facts."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelCommandInvocation(BaseModel):
    """A named sender; on-demand use is an explicit policy, never a default.

    ``automatic`` records a declared producer, not evidence that it ran.
    ``automatic_required`` retains a missing-trigger obligation even when an
    operator diagnostic entry point is deliberately kept available.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    mode: Literal["automatic", "on_demand"]
    sender: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    evidence: str | None = Field(default=None, min_length=1)
    trigger: Literal["contract", "schedule_or_hook", "code"] | None = None
    automatic_required: bool = False
