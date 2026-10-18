# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input of the pinned transcript read effect (OMN-21129, plan task TC.6)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelTranscriptReadRequest(BaseModel):
    """A transcript path relative to ONEX_CONTEXT_REPLAY_ROOT."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    relative_path: str = Field(min_length=1)
