# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One reading of one seam over one window.

``crossings`` counts the crossings that could be read (matched plus
mismatched); an unreadable crossing is counted in ``unreadable`` only, so a
window of nothing but unreadable crossings has zero crossings.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Self

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.seam_reading.enum_seam_crossing_state import (
    EnumSeamCrossingState,
)
from omnimarket.models.seam_reading.model_seam_crossing import ModelSeamCrossing
from omnimarket.models.seam_reading.model_seam_window import ModelSeamWindow

MAX_EXAMPLES = 3


class ModelSeamReading(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seam_id: str
    window: ModelSeamWindow
    crossings: int = Field(ge=0)
    mismatched: int = Field(ge=0)
    unreadable: int = Field(ge=0)
    examples: tuple[str, ...] = Field(default=(), max_length=MAX_EXAMPLES)
    read_at: datetime

    @classmethod
    def from_crossings(
        cls,
        seam_id: str,
        window: ModelSeamWindow,
        crossings: Sequence[ModelSeamCrossing],
        read_at: datetime,
    ) -> Self:
        mismatched = [
            c.key for c in crossings if c.state is EnumSeamCrossingState.MISMATCHED
        ]
        unreadable = sum(
            1 for c in crossings if c.state is EnumSeamCrossingState.UNREADABLE
        )
        return cls(
            seam_id=seam_id,
            window=window,
            crossings=len(crossings) - unreadable,
            mismatched=len(mismatched),
            unreadable=unreadable,
            examples=tuple(mismatched[:MAX_EXAMPLES]),
            read_at=read_at,
        )
