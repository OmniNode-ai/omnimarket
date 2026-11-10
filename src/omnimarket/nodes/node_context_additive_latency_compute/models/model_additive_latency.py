# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed round trips in, latency table and proposed budgets out (OMN-21134, TC.12)."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.claude_code_binding import ADDITIVE_BUDGET_CAPS_MS

MIN_SAMPLES_PER_CELL = 20


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnumAdditiveRoundTripTemperature(StrEnum):
    COLD = "cold"
    WARM = "warm"


_Duration = Annotated[float, Field(ge=0, allow_inf_nan=False)]


def _require_additive_profile(profile: str) -> str:
    if profile not in ADDITIVE_BUDGET_CAPS_MS:
        raise ValueError(
            f"additive profile {profile!r} is not one of "
            f"{sorted(ADDITIVE_BUDGET_CAPS_MS)}"
        )
    return profile


class ModelAdditiveRoundTrip(_Frozen):
    """One recorded round trip of an additive profile through the adapter."""

    round_trip_id: str = Field(min_length=1)
    profile: str = Field(min_length=1)
    host: str = Field(min_length=1)
    temperature: EnumAdditiveRoundTripTemperature
    duration_ms: _Duration = Field(
        description="Hook entry to package returned, as the adapter measured it."
    )

    @model_validator(mode="after")
    def _known_profile(self) -> Self:
        _require_additive_profile(self.profile)
        return self


class ModelAdditiveLatencyRequest(_Frozen):
    round_trips: tuple[ModelAdditiveRoundTrip, ...] = Field(min_length=1)
    required_profiles: tuple[str, ...] = Field(min_length=1)
    required_hosts: tuple[str, ...] = Field(min_length=2)
    min_samples_per_cell: int = Field(
        default=MIN_SAMPLES_PER_CELL, ge=MIN_SAMPLES_PER_CELL
    )
    candidate_budgets_ms: dict[str, int] = Field(
        default_factory=dict,
        description="Budgets a data PR proposes, by profile; each is checked.",
    )

    @model_validator(mode="after")
    def _distinct_and_known(self) -> Self:
        ids = [t.round_trip_id for t in self.round_trips]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate round_trip_id")
        if len(set(self.required_profiles)) != len(self.required_profiles):
            raise ValueError("duplicate required profile")
        if len(set(self.required_hosts)) != len(self.required_hosts):
            raise ValueError("duplicate required host")
        for profile in self.required_profiles:
            _require_additive_profile(profile)
        for profile, budget in self.candidate_budgets_ms.items():
            if profile not in self.required_profiles:
                raise ValueError(
                    f"candidate budget for {profile!r}: not a required profile"
                )
            if budget <= 0:
                raise ValueError(f"candidate budget for {profile!r} must be above zero")
        return self


class ModelAdditiveLatencyStats(_Frozen):
    p50_ms: float
    p95_ms: float
    max_ms: float


class ModelAdditiveLatencyCell(_Frozen):
    profile: str
    host: str
    temperature: EnumAdditiveRoundTripTemperature
    count: int
    latency: ModelAdditiveLatencyStats


class ModelAdditiveHostVerdict(_Frozen):
    """One host's worst p95 for a profile against that profile's cap."""

    host: str
    p95_ms: float | None
    promotable: bool
    remedy: str = Field(
        description="Empty when promotable, or when the table is incomplete."
    )


class ModelAdditiveProfileProposal(_Frozen):
    profile: str
    cap_ms: int
    worst_p95_ms: float | None
    proposed_budget_ms: int | None
    hosts: tuple[ModelAdditiveHostVerdict, ...]


class ModelAdditiveLatencyResult(_Frozen):
    """Cells and per-profile proposals; proposals are withheld when incomplete."""

    cells: tuple[ModelAdditiveLatencyCell, ...]
    complete: bool
    proposals: tuple[ModelAdditiveProfileProposal, ...]
    falsifiers: tuple[str, ...]
