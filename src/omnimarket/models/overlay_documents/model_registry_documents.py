# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for registry configuration that belongs to no single node (OMN-22478).

Each is what the overlay home validates a document through before holding it, and imports pydantic
alone, because that check installs pydantic and pyyaml and nothing else. No value lives here: the
documents are private deployment data and stay in the overlay home.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelRegistryRepositoryLanding(BaseModel):
    """How a repository lands: one pull request at a time, against this required check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    strict_base: bool
    required_context: str = Field(min_length=1)


class ModelRegistryRepository(BaseModel):
    """One repository a registry root clones."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    repo: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    type: str = Field(min_length=1)
    description: str = Field(min_length=1)
    landing: ModelRegistryRepositoryLanding | None = None


class ModelRepositoryRegistryOverlay(BaseModel):
    """The registry root's clone list."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repositories: tuple[ModelRegistryRepository, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _names_are_unique(self) -> Self:
        names = [entry.name for entry in self.repositories]
        repeated = sorted({name for name in names if names.count(name) > 1})
        if repeated:
            raise ValueError(f"repository names repeat: {repeated}")
        return self


class ModelLaneRulesOverlay(BaseModel):
    """The standing rules block every lane brief embeds, as one document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    rules_block: str = Field(min_length=1)


class ModelPillarOwner(BaseModel):
    """Who a pillar's new tickets are assigned to."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    owner_linear_user_id: str = Field(min_length=1)
    owner_github_login: str | None = None
    assignable_by_lanes: bool = True


class ModelPillarOwnersOverlay(BaseModel):
    """Ticket assignment by pillar."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pillars: dict[str, ModelPillarOwner] = Field(min_length=1)


class ModelVocabularyOverlay(BaseModel):
    """A vocabulary: named lists of strings, with resolved-at stamps and a cache age beside them.

    The list names differ per vocabulary and belong to their readers, so the key set is open, but
    every value is still checked: a list of strings, or a stamp (``*_at``) or a day count
    (``*_days``). A key of any other kind refuses rather than passing unread. At least one list.
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    schema_version: Literal[1]

    @model_validator(mode="after")
    def _values_are_closed(self) -> Self:
        extras = self.model_extra or {}
        lists = 0
        for key, value in extras.items():
            if key.endswith("_at") and isinstance(value, (date, datetime)):
                continue
            if (
                key.endswith("_days")
                and isinstance(value, int)
                and not isinstance(value, bool)
            ):
                continue
            if isinstance(value, list) and all(isinstance(item, str) for item in value):
                lists += 1
                continue
            raise ValueError(
                f"vocabulary key {key} is neither a list of strings nor a stamp"
            )
        if lists == 0:
            raise ValueError("a vocabulary holds at least one list")
        return self


__all__ = [
    "ModelLaneRulesOverlay",
    "ModelPillarOwner",
    "ModelPillarOwnersOverlay",
    "ModelRegistryRepository",
    "ModelRegistryRepositoryLanding",
    "ModelRepositoryRegistryOverlay",
    "ModelVocabularyOverlay",
]
