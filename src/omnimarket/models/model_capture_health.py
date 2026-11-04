# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Measured classifier calls and the silent-failure alarm's public wire types."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelClassifierCall(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Literal[
        "answered", "not-called", "unreachable", "timeout", "empty", "invalid", "failed"
    ]
    command: str = Field(min_length=1)
    observation: str = Field(min_length=1)


class ModelClassifierObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    capture_id: str = Field(min_length=1)
    classifier: str
    call: ModelClassifierCall | None = None


class ModelCaptureHealthPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    unanswered_window: int = Field(ge=1)
    fallback_classifier: str
    alarm: str
    finding_lane: str
    finding_kind: str
    fix: str


class ModelCaptureHealthRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    observations: tuple[ModelClassifierObservation, ...]
    policy: ModelCaptureHealthPolicy


class ModelCaptureHealthFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["FINDING"] = "FINDING"
    alarm: str
    episode: str
    unanswered: int
    why: str
    fix: str


class ModelCaptureHealthResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    findings: tuple[ModelCaptureHealthFinding, ...] = ()
    causes: dict[str, int] = Field(default_factory=dict)
    measurements: tuple[ModelClassifierCall, ...] = ()
