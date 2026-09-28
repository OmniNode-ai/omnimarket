# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One append to ``pr_landing_transitions``, keyed (repository, pr_number, seq)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.events.pr_landing.enum_pr_landing_state import (
    EnumPrLandingState,
)
from omnimarket.events.pr_landing.model_pr_landing_intent import (
    ModelPrLandingIntent,
)
from omnimarket.events.pr_landing.model_pr_landing_observation import (
    HEAD_SHA_PATTERN,
    REPOSITORY_PATTERN,
)


class ModelPrLandingTransitionRow(BaseModel):
    """One transition, as the orchestrator published it. Never updated."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository: str = Field(..., pattern=REPOSITORY_PATTERN)
    pr_number: int = Field(..., ge=1)
    seq: int = Field(..., ge=0)
    head_sha: str | None = Field(default=None, pattern=HEAD_SHA_PATTERN)
    from_state: EnumPrLandingState | None = Field(...)
    to_state: EnumPrLandingState = Field(...)
    trigger: str = Field(..., min_length=1)
    intents: tuple[ModelPrLandingIntent, ...] = Field(default=())
    opens_episode: bool = Field(...)
    transitioned_at: datetime = Field(...)

    @model_validator(mode="after")
    def _transitioned_at_carries_a_timezone(self) -> Self:
        if self.transitioned_at.utcoffset() is None:
            msg = "transitioned_at must carry a timezone"
            raise ValueError(msg)
        return self

    def intents_json(self) -> str:
        """The intents as canonical JSON: the same transition, the same bytes."""
        return json.dumps(
            [intent.model_dump(mode="json") for intent in self.intents],
            sort_keys=True,
            separators=(",", ":"),
        )


__all__: list[str] = ["ModelPrLandingTransitionRow"]
