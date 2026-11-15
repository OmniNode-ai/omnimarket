# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the one-copy check: the overlay, one referrer, the request and the result."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ReferrerKind = Literal["unit", "tick", "source"]


def _markers_are_text(value: tuple[str, ...]) -> tuple[str, ...]:
    if any(not marker.strip() or marker != marker.strip() for marker in value):
        raise ValueError("a marker is non-empty text with no edge whitespace")
    return value


class ModelImplementation(BaseModel):
    """One implementation of a behaviour; it is live when a marker is a substring of an installed unit's or a tick's text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    markers: tuple[str, ...] = Field(min_length=1)

    _markers = field_validator("markers")(_markers_are_text)


class ModelBehaviour(BaseModel):
    """A behaviour that must exist once; ``fix_landed_in`` names the implementation the latest fix landed in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    implementations: tuple[ModelImplementation, ...] = Field(min_length=1)
    fix_landed_in: str | None = None

    @model_validator(mode="after")
    def _ids_are_unique_and_the_fix_is_one_of_them(self) -> ModelBehaviour:
        ids = [implementation.id for implementation in self.implementations]
        if len(set(ids)) != len(ids):
            raise ValueError(
                f"behaviour {self.name}: implementation ids are not unique"
            )
        if self.fix_landed_in is not None and self.fix_landed_in not in ids:
            raise ValueError(
                f"behaviour {self.name}: fix_landed_in {self.fix_landed_in!r} is not an implementation id"
            )
        return self


class ModelResource(BaseModel):
    """A resource with several possible writers: a source that holds a write marker must hold a lock marker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    write_markers: tuple[str, ...] = Field(min_length=1)
    lock_markers: tuple[str, ...] = Field(min_length=1)
    comment_prefixes: tuple[str, ...]

    _write = field_validator("write_markers")(_markers_are_text)
    _lock = field_validator("lock_markers")(_markers_are_text)


class ModelReferrerSource(BaseModel):
    """One thing the file reader reads under the registry root; ``yaml_key`` picks a subtree of a YAML file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    kind: ReferrerKind
    path: str = Field(min_length=1)
    yaml_key: str | None = None


class ModelOneCopyCheckOverlay(BaseModel):
    """The node overlay, supplied by the deployment; the package ships no behaviour, marker, resource or path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    emission_piece: int = Field(ge=1, le=18)
    behaviours: tuple[ModelBehaviour, ...]
    resources: tuple[ModelResource, ...]
    referrer_sources: tuple[ModelReferrerSource, ...] = Field(min_length=1)


class ModelReferrer(BaseModel):
    """An installed unit's declaration, a tick's source or a writer's source, as text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(min_length=1)
    kind: ReferrerKind
    text: str


class ModelOneCopyCheckRequest(BaseModel):
    """``as_of`` is the instant the result is stamped with; unset means the injected clock's reading (a replay sets it)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: datetime | None = None


class ModelLiveImplementation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    referrers: tuple[str, ...]


class ModelOneCopyFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    subject: str
    detail: str


class ModelBehaviourReading(BaseModel):
    """What one behaviour reads: the live implementations with the referrers that run them, and the copies nothing runs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    live: tuple[ModelLiveImplementation, ...]
    unreferenced: tuple[str, ...]
    fix_landed_in: str | None


class ModelResourceReading(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    writers: tuple[str, ...]
    writers_without_lock: tuple[str, ...]


class ModelOneCopyCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: datetime
    ok: bool
    behaviours: tuple[ModelBehaviourReading, ...]
    resources: tuple[ModelResourceReading, ...]
    findings: tuple[ModelOneCopyFinding, ...]
