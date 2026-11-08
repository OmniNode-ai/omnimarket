# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and overlay of the customer health line (OMN-22923, piece 17b).

Every setting is the overlay's: which pieces make up the customer path, the title of the line and how long the line
itself may be drawn as current. The readings are the ones the floor record's piece rows already carry.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.floor_17a.model_floor_17a import ReadingState


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelHealthReadingIn(_Frozen):
    """One piece row of the floor record: the piece, its reading state, the reading's age and its own maximum age."""

    piece: int = Field(ge=1, le=18)
    name: str
    reading_state: ReadingState | None
    reading_age_s: int | None
    reading_max_age_s: int | None


class ModelCustomerHealthOverlay(_Frozen):
    """The overlay of the customer health line (``node_product_floor_verdict_compute``)."""

    title: str = Field(min_length=1)
    required_pieces: tuple[int, ...] = Field(min_length=1)
    max_age_s: int = Field(gt=0)

    @model_validator(mode="after")
    def _pieces(self) -> ModelCustomerHealthOverlay:
        if len(set(self.required_pieces)) != len(self.required_pieces):
            raise ValueError("required_pieces names each piece once")
        if any(not 1 <= p <= 18 for p in self.required_pieces):
            raise ValueError("required_pieces names pieces 1 to 18")
        return self


class ModelCustomerHealthRequest(_Frozen):
    now: datetime
    readings: tuple[ModelHealthReadingIn, ...]
    overlay: ModelCustomerHealthOverlay
