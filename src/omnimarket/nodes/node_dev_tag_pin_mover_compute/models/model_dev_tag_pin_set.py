# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed I/O for the co-resolving pin-set selection of the nightly train (OMN-21895)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumPinSetOutcome(StrEnum):
    """What the night's selection decided. Only ``moved`` opens a PR."""

    MOVED = "moved"
    UNCHANGED = "unchanged"
    NO_CO_RESOLVING_SET = "no_co_resolving_set"


class ModelPinSetCandidate(BaseModel):
    """One dev tag of a sibling, newest first in its sibling's list.

    ``requires`` is what that tag's own ``pyproject.toml`` pins for the other
    siblings of the train: distribution -> dev tag. uv honours a git
    dependency's sources, so a consumer that pins this tag must pin the same
    tag for each of them.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tag: str
    green: bool | None = None
    requires: dict[str, str] = Field(default_factory=dict)


class ModelPinSetSibling(BaseModel):
    """A sibling the consumer pins by dev tag, with the night's candidates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    distribution: str
    repository_url: str
    current_tag: str
    current_requires: dict[str, str] = Field(default_factory=dict)
    candidates: tuple[ModelPinSetCandidate, ...] = ()


class ModelPinSetRequest(BaseModel):
    """Every dev-tag-pinned sibling of one consumer and its candidates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    consumer: str
    siblings: tuple[ModelPinSetSibling, ...]


class ModelPinSetMove(BaseModel):
    """One sibling moved by the night's bump, with every dev tag it rolls in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    distribution: str
    repository_url: str
    old_tag: str
    new_tag: str
    rolled: tuple[str, ...] = ()
    # The sibling tags the new tag's own pyproject.toml pins, as the night
    # moved them together: distribution -> dev tag.
    requires: dict[str, str] = Field(default_factory=dict)


class ModelPinSetConflict(BaseModel):
    """Two pins uv cannot resolve together for one package."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    package: str
    urls: tuple[str, ...]
    detail: str

    def render(self) -> str:
        lines = [
            f"Requirements contain conflicting URLs for package `{self.package}`:",
            *(f"- {url}" for url in self.urls),
            self.detail,
        ]
        return "\n".join(lines)


class ModelPinSetHeldBack(BaseModel):
    """A sibling's newest green tag the night did not take, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    distribution: str
    tag: str
    conflict: ModelPinSetConflict


class ModelPinSetResult(BaseModel):
    """The selected tag set, or the refusal with the conflicting URLs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: EnumPinSetOutcome
    moves: tuple[ModelPinSetMove, ...] = ()
    unchanged: tuple[str, ...] = ()
    held_back: tuple[ModelPinSetHeldBack, ...] = ()
    detail: str = ""

    @property
    def conflicts(self) -> tuple[ModelPinSetConflict, ...]:
        return tuple(item.conflict for item in self.held_back)
