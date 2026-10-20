# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing stall fold's request and result."""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.models.liveness.model_automation_liveness import (
    ModelTriggerExpectation,
    NonEmptyStr,
    ProcessId,
)
from omnimarket.models.liveness.model_landing_stall import (
    EnumLandingStallSubject,
    ModelLandingStallTransition,
)


class ModelLandingStallRoute(BaseModel):
    """The overlay entry that judges one subject's stalls."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: EnumLandingStallSubject
    process_id: ProcessId
    host: NonEmptyStr


class ModelLandingStallFoldRequest(BaseModel):
    """The controller's transitions in a window, and where each subject goes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    transitions: tuple[ModelLandingStallTransition, ...] = ()
    routes: tuple[ModelLandingStallRoute, ...]

    @model_validator(mode="after")
    def _one_route_per_subject(self) -> Self:
        subjects = [r.subject for r in self.routes]
        if len(subjects) != len(set(subjects)):
            raise ValueError("one route per landing stall subject")
        return self


class ModelLandingStallFoldResult(BaseModel):
    """One expectation per held subject: its latest stall, answered or not."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    expectations: tuple[ModelTriggerExpectation, ...] = ()
    #: Match keys of transitions whose subject has no route; never dropped silently.
    unrouted: tuple[str, ...] = ()


__all__ = [
    "ModelLandingStallFoldRequest",
    "ModelLandingStallFoldResult",
    "ModelLandingStallRoute",
]
