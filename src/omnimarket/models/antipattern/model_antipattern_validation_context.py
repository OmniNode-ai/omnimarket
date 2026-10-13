# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Context that rides along one antipattern validation through its bus hops."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAntipatternValidationContext(BaseModel):
    """What the orchestrator needs back when a hop's answer returns.

    The match effect and the classifier answer with the correlation id and their
    own result only. The orchestrator holds no state between hops, so the file
    under validation and the policy it asked for travel on the request and are
    echoed unchanged on the answer. ``match_error`` is the one field a hop fills:
    the orchestrator copies the match effect's non-fatal ``error_message`` into it
    before the classify hop, so a graceful skip stays visible on the terminal event.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    file_path: str
    line_count: int = Field(ge=0)
    similarity_threshold: float = Field(ge=0.0, le=1.0)
    enforcement_mode: str
    match_error: str | None = None


__all__ = ["ModelAntipatternValidationContext"]
