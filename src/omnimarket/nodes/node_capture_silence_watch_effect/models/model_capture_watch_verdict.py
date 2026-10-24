# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The finding for a source or one of its active principals."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from omnimarket.nodes.node_capture_silence_watch_effect.models.enum_capture_watch_state import (
    EnumCaptureWatchState,
)


class ModelCaptureWatchVerdict(BaseModel):
    """Unobserved values remain None on UNKNOWN findings."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    source_id: str
    scope: Literal["actor", "source"]
    actor: str | None
    state: EnumCaptureWatchState
    newest_at: AwareDatetime | None
    age_seconds: float | None
    bound_seconds: int
    error: str | None = None
