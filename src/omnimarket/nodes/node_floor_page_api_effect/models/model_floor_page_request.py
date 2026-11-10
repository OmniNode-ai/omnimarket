# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and response of one floor route (OMN-23072)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelFloorRoute(BaseModel):
    """One document the mount serves, as the contract declares it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    path: str
    document: str


class ModelFloorRefusal(BaseModel):
    """A named refusal the contract declares."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: int
    code: str
    detail: str


class ModelFloorPageRequest(BaseModel):
    """Which declared document is asked for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    document: str = Field(description="The document kind of a declared floor route.")


class ModelFloorPageResponse(BaseModel):
    """The answer: the document body with the time it was computed, or a named refusal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status_code: int
    body: dict[str, Any]
    computed_at: str | None = None
