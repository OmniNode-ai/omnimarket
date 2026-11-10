# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Temporary desired-state layers with lifetimes supplied by overlay data."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from omnimarket.models.model_host_desired_state import ModelHostDesiredEntry
from omnimarket.models.model_host_wire import ModelHostWire, NonEmptyStr, UtcStamp


class ModelLayerBounds(ModelHostWire):
    """Required overlay limits for temporary installation layers."""

    default_life_hours: int = Field(gt=0)
    max_life_days: int = Field(gt=0)

    @model_validator(mode="after")
    def _default_fits_maximum(self) -> Self:
        """Keep the supplied default within the supplied maximum lifetime."""
        if self.default_life_hours > self.max_life_days * 24:
            raise ValueError("default_life_hours exceeds max_life_days in hours")
        return self


class ModelHostLayerRequest(ModelHostWire):
    """A request to apply a temporary layer against an expected revision."""

    request_id: NonEmptyStr
    layer_id: NonEmptyStr
    expected_revision: NonEmptyStr
    entries: Annotated[tuple[ModelHostDesiredEntry, ...], Field(min_length=1)]
    requested_by: NonEmptyStr
    requested_at: UtcStamp
    life_hours: int | None = Field(default=None, gt=0)

    @property
    def key(self) -> str:
        """Address the requested layer."""
        return self.layer_id

    def resolved_life_hours(self, bounds: ModelLayerBounds) -> int:
        """Resolve the lifetime using the caller's overlay default and maximum."""
        hours = (
            self.life_hours
            if self.life_hours is not None
            else bounds.default_life_hours
        )
        if hours > bounds.max_life_days * 24:
            raise ValueError("layer life_hours exceeds max_life_days in hours")
        return hours


class ModelHostLayerDecision(ModelHostWire):
    """The accepted, refused, or expired disposition of a layer request."""

    layer_id: NonEmptyStr
    request_id: NonEmptyStr
    decision: Literal["accepted", "refused", "expired"]
    reason: str
    decided_at: UtcStamp
    expires_at: UtcStamp | None = None
    generation: int | None = None

    @property
    def key(self) -> str:
        """Address the decided layer."""
        return self.layer_id


class ModelHostLayerTombstone(ModelHostWire):
    """The recorded removal of a temporary layer."""

    layer_id: NonEmptyStr
    removed_at: UtcStamp
    reason: str

    @property
    def key(self) -> str:
        """Address the removed layer."""
        return self.layer_id
