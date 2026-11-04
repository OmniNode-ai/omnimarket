# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The read surface's declared values: topic, freshness threshold, wait pacing.

They are declared in the reading node's contract and loaded once; no handler
carries them as literals.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.pr_state import EnumPrState


class ModelPrStateReadPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    projection_topic: str = Field(min_length=1)
    max_row_age_seconds: int = Field(gt=0)
    wait_poll_seconds: float = Field(gt=0)
    terminal_verdicts: tuple[Literal["GREEN", "RED", "PENDING", "NONE"], ...] = Field(
        min_length=1
    )
    terminal_states: tuple[EnumPrState, ...]
