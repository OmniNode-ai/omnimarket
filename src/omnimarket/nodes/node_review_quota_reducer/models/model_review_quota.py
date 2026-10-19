# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, state, decision and preregistration views of the review quota reducer (OMN-21187)."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.enums.enum_review_quota import (
    EnumReviewQuotaCommand,
    EnumReviewQuotaEntryStatus,
    EnumReviewQuotaReason,
)


class RouteNotEligibleError(LookupError):
    """The route is not on the preregistered list."""


class ModelReviewQuotaRoute(BaseModel):
    """One preregistered route and its weekly quota."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    route_id: str = Field(..., min_length=1)
    tier: str
    feeds_rubric: bool
    weekly_quota: int = Field(..., ge=1)


class ModelReviewQuotaCollection(BaseModel):
    """The preregistered collection window."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    window_days: int = Field(..., ge=1)
    pilot_days: int = Field(..., ge=0)
    quota_window_days: int = Field(..., ge=1)


class ModelReviewQuotaPreregistration(BaseModel):
    """The parts of the sealed preregistration the reducer reads, with the file's hash."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    collection: ModelReviewQuotaCollection
    eligible_routes: tuple[ModelReviewQuotaRoute, ...]

    def is_eligible(self, route_id: str) -> bool:
        """Whether the route is on the preregistered list."""
        return any(route.route_id == route_id for route in self.eligible_routes)

    def quota_for(self, route_id: str) -> int:
        """The route's weekly quota, or RouteNotEligibleError when it is not on the list."""
        for route in self.eligible_routes:
            if route.route_id == route_id:
                return route.weekly_quota
        raise RouteNotEligibleError(
            f"Route {route_id!r} is not on the preregistered list"
        )


class ModelReviewQuotaEntry(BaseModel):
    """One recorded request: it counts against its route's quota window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: str = Field(..., min_length=1)
    route_id: str = Field(..., min_length=1)
    window_index: int = Field(..., ge=0)
    status: EnumReviewQuotaEntryStatus


class ModelReviewQuotaState(BaseModel):
    """Every request the reducer has admitted and not released."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    entries: tuple[ModelReviewQuotaEntry, ...] = ()


class ModelReviewQuotaRequest(BaseModel):
    """One command on one request id, with the clock, the collection start and the prior state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: EnumReviewQuotaCommand
    request_id: str = Field(..., min_length=1)
    route_id: str = Field(..., min_length=1)
    as_of: AwareDatetime
    collection_started_at: AwareDatetime | None = None
    preregistration_sha256: str = Field(..., pattern=r"^[0-9a-f]{64}$")
    state: ModelReviewQuotaState = ModelReviewQuotaState()


class ModelReviewQuotaDecision(BaseModel):
    """Whether the command was applied, why, and the state after it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    applied: bool
    reason: EnumReviewQuotaReason
    request_id: str
    route_id: str
    window_index: int | None = None
    phase: Literal["pilot", "main"] | None = None
    quota: int | None = None
    used_in_window: int | None = None
    state: ModelReviewQuotaState
    preregistration_sha256: str
