# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One HTTP route the projection query node serves, as its contract declares it (OMN-21288)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EnumQueryRouteKind(StrEnum):
    """What a route answers; the handler dispatches on this and on nothing else."""

    HEALTH = "health"
    READY = "ready"
    GENERATE = "generate"
    PROJECTIONS = "projections"
    STATUS_PAGE = "status_page"
    PROJECTION_PAGE = "projection_page"
    EVIDENCE_PAGE = "evidence_page"
    EVIDENCE_STREAM = "evidence_stream"


class ModelQueryParam(BaseModel):
    """One query parameter of a route: its type and the constraints that refuse a bad value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    type: Literal["str", "int"]
    default: int | None = Field(
        default=None,
        description="Integer default; a parameter with none is optional and absent.",
    )
    ge: int | None = None
    le: int | None = None
    pattern: str | None = None


class ModelQueryRoute(BaseModel):
    """A route: its methods and paths, what it answers and the inputs it reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    kind: EnumQueryRouteKind
    methods: tuple[Literal["GET", "POST"], ...]
    paths: tuple[str, ...]
    query: tuple[ModelQueryParam, ...] = ()
    dependencies: tuple[Literal["topic_map", "row_source"], ...] = ()
    topic: str | None = Field(
        default=None, description="The projection topic an evidence route reads."
    )
    correlation_source: str | None = Field(
        default=None,
        description="The projection topic an evidence page names as its authoritative correlation source.",
    )
    body_model: str | None = Field(
        default=None, description="Dotted path of the request body model, if any."
    )
    media_type: Literal["application/json", "text/html", "text/event-stream"] = (
        "application/json"
    )


class ModelQueryMount(BaseModel):
    """The contract's http_mount block, as the runtime reads it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    factory: str
    path_prefix: str


__all__ = [
    "EnumQueryRouteKind",
    "ModelQueryMount",
    "ModelQueryParam",
    "ModelQueryRoute",
]
