# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and result of the lane-text gh-read scan (OMN-18946)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelGhReadSurface(BaseModel):
    """One lane-facing text: a brief, a skill, a script or a prompt template."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    text: str
    kind: Literal["prose", "code"] = "prose"


class ModelGhReadScanRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = ""
    surfaces: tuple[ModelGhReadSurface, ...]


class ModelGhReadFinding(BaseModel):
    """A line that tells a lane to run a GitHub read or write through gh, with the mirror that answers it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    line: int = Field(ge=1)
    shape: str
    command: str
    mirror: str


class ModelGhReadScanResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    findings: tuple[ModelGhReadFinding, ...]
