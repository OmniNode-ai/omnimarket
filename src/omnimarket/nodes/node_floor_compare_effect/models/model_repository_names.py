# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed model of the repository names overlay (OMN-23134).

Every source of the compare history spells a repository the way it holds it: the canonical clone by its directory, the pull
request watcher by the GitHub name. The overlay names, per repository, the registry's canonical name and every other
spelling that reads the same repository (an underscore and a hyphen spelling, the old and the new name of a renamed
repository). The registry's own clone list supplies the names it spells two ways; the overlay supplies renames. The compare
reads every per-repository key under the canonical name; nothing in code names a repository.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelRepositoryKeyedFamily(_Frozen):
    """A family of history keys whose suffix after ``prefix`` is a repository name."""

    prefix: str = Field(min_length=1)
    total_key: str | None = Field(
        default=None,
        description="The key holding the sum of the family over every repository read; a repository read under two spellings is counted once in it.",
    )


class ModelRepositoryNameEntry(_Frozen):
    canonical: str = Field(
        min_length=1, description="The registry's name for the repository."
    )
    aliases: tuple[str, ...] = Field(min_length=1)
    evidence: str = Field(
        min_length=1, description="Why the aliases read the same repository."
    )


class ModelRepositoryNames(_Frozen):
    families: tuple[ModelRepositoryKeyedFamily, ...] = Field(min_length=1)
    registry_relpath: str | None = Field(
        default=None,
        description="The registry's clone list, relative to the registry home: each entry's name is canonical and the last segment of its repo is an alias when it differs.",
    )
    repositories: tuple[ModelRepositoryNameEntry, ...] = ()

    @model_validator(mode="after")
    def _each_spelling_names_one_repository(self) -> ModelRepositoryNames:
        owners: dict[str, str] = {}
        for entry in self.repositories:
            for name in (entry.canonical, *entry.aliases):
                if name in owners:
                    raise ValueError(
                        f"{name!r} is listed twice: under {owners[name]!r} and under {entry.canonical!r}"
                    )
                owners[name] = entry.canonical
        return self
