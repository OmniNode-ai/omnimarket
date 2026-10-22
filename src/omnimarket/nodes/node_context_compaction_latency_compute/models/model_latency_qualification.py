# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed round trips in, latency table and proposed deadlines out (OMN-21114, TA.12)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.context_compaction import ModelContextCompactionPrepared

MIN_SAMPLES_PER_CELL = 20
DEFAULT_PRECOMPUTE_SLACK_MS = 5000


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumRoundTripTemperature(StrEnum):
    COLD = "cold"
    WARM = "warm"


_Duration = Annotated[float, Field(ge=0, allow_inf_nan=False)]


class ModelCompactionRoundTrip(_Frozen):
    """One recorded round trip, its two phases timed separately."""

    round_trip_id: str = Field(min_length=1)
    size: str = Field(min_length=1)
    host: str = Field(min_length=1)
    temperature: EnumRoundTripTemperature
    deterministic_build_ms: _Duration
    model_section_ms: _Duration

    @classmethod
    def from_prepared(
        cls,
        prepared: ModelContextCompactionPrepared,
        *,
        round_trip_id: str,
        size: str,
        host: str,
        temperature: EnumRoundTripTemperature,
    ) -> Self:
        """Split a prepared receipt's build into deterministic and model-written time."""
        if prepared.build_ms is None:
            raise ValueError("prepared.build_ms is required")
        if prepared.model_section_ms is None:
            raise ValueError("prepared.model_section_ms is required")
        if prepared.model_section_ms > prepared.build_ms:
            raise ValueError("prepared.model_section_ms exceeds prepared.build_ms")
        return cls(
            round_trip_id=round_trip_id,
            size=size,
            host=host,
            temperature=temperature,
            deterministic_build_ms=prepared.build_ms - prepared.model_section_ms,
            model_section_ms=prepared.model_section_ms,
        )


class ModelLatencyQualificationRequest(_Frozen):
    round_trips: tuple[ModelCompactionRoundTrip, ...] = Field(min_length=1)
    required_sizes: tuple[str, ...] = Field(min_length=3)
    required_hosts: tuple[str, ...] = Field(min_length=2)
    margin_ms: int = Field(gt=0)
    precompute_slack_ms: int = Field(default=DEFAULT_PRECOMPUTE_SLACK_MS, ge=0)
    min_samples_per_cell: int = Field(
        default=MIN_SAMPLES_PER_CELL, ge=MIN_SAMPLES_PER_CELL
    )
    candidate_class_ceiling_ms: int | None = Field(default=None, ge=0)
    candidate_precompute_deadline_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        ids = [t.round_trip_id for t in self.round_trips]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate round_trip_id")
        if len(set(self.required_sizes)) != len(self.required_sizes):
            raise ValueError("duplicate required size")
        if len(set(self.required_hosts)) != len(self.required_hosts):
            raise ValueError("duplicate required host")
        return self


class ModelLatencyStats(_Frozen):
    p50_ms: float
    p95_ms: float
    max_ms: float


class ModelLatencyCell(_Frozen):
    size: str
    host: str
    temperature: EnumRoundTripTemperature
    count: int
    deterministic_build: ModelLatencyStats
    model_section: ModelLatencyStats
    total: ModelLatencyStats


class ModelLatencyQualificationResult(_Frozen):
    """Cells, the worst total p95, and the proposals; proposals are None when incomplete."""

    cells: tuple[ModelLatencyCell, ...]
    complete: bool
    build_p95_ms: float | None
    class_ceiling_ms: int | None
    precompute_deadline_ms: int | None
    falsifiers: tuple[str, ...]
