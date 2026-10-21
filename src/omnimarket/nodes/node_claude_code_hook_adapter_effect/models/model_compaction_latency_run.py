# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recorded cases and raw adapter latency trips (OMN-18531 TA.12, OMN-17427)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumCompactionLatencyTemperature(StrEnum):
    COLD = "cold"
    WARM = "warm"


class ModelCompactionLatencyCase(_Frozen):
    """One recorded PreCompact hook input, labeled by size."""

    size: str = Field(min_length=1)
    hook_input: dict[str, JsonValue]


class ModelCompactionLatencyRunRequest(_Frozen):
    """Run each size on a host with fresh cold clients and a shared warm client."""

    host: str = Field(min_length=1)
    cases: tuple[ModelCompactionLatencyCase, ...] = Field(min_length=1)
    cold: int = Field(default=20, ge=0)
    warm: int = Field(default=20, ge=0)
    deadline_ms: int = Field(default=30000, gt=0)

    @model_validator(mode="after")
    def _distinct_sizes(self) -> Self:
        sizes = [case.size for case in self.cases]
        if len(sizes) != len(set(sizes)):
            raise ValueError("duplicate case size")
        return self


_Duration = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class ModelCompactionLatencyTrip(_Frozen):
    """An adapter round trip with a measured phase split or an explicit refusal."""

    round_trip_id: str
    size: str
    host: str
    temperature: EnumCompactionLatencyTemperature
    wall_ms: _Duration
    deterministic_build_ms: _Duration | None
    model_section_ms: _Duration | None
    verdict_action: str | None
    refusal: str | None

    @model_validator(mode="after")
    def _split_or_refusal(self) -> Self:
        has_build = self.deterministic_build_ms is not None
        has_model = self.model_section_ms is not None
        if self.refusal is None:
            if not (has_build and has_model):
                raise ValueError("both phase timings are required without a refusal")
        elif has_build or has_model:
            raise ValueError("phase timings must be absent with a refusal")
        return self


class ModelCompactionLatencyRunResult(_Frozen):
    """Raw trips for the qualification compute to tabulate."""

    host: str
    trips: tuple[ModelCompactionLatencyTrip, ...]
    refusal_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _count_refusals(self) -> Self:
        if self.refusal_count != sum(trip.refusal is not None for trip in self.trips):
            raise ValueError("refusal_count must equal the number of refused trips")
        return self
