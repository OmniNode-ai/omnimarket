# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing watcher facts node's deployment overlay: every setting, no code default for a deployment fact (OMN-23507)."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.models.landing_decision import REPO_PATTERN, EnumLandingRedClass

OVERLAY_ID = "node_pr_landing_watcher_facts_effect"

_FILE_FIELDS = ("watcher_state_path", "facts_path", "emission_journal")


class EnumWatcherFactsBackend(StrEnum):
    INMEMORY = "inmemory"
    FILE = "file"


class ModelWatcherFactsOverlay(BaseModel):
    """<root>/node_pr_landing_watcher_facts_effect/overlay.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: EnumWatcherFactsBackend = Field(
        description="Where the state is read and the facts are written: in memory, or files."
    )
    repositories: tuple[str, ...] = Field(
        min_length=1,
        description=(
            "The repositories (bare names, under owner) whose open pull requests are written: "
            "the ones switched to the composed landing workflow, which the old controller gives no facts."
        ),
    )
    owner: str = Field(
        min_length=1, description="The owner the pull request keys are under."
    )
    unclassified_red_class: EnumLandingRedClass = Field(
        description=(
            "The red class a red head is given: the watcher's state carries no classification, "
            "and the landing facts need one for every red head."
        )
    )
    written_topic: str = Field(min_length=1, description="The written event's topic.")
    failed_topic: str = Field(min_length=1, description="The failed event's topic.")
    floor_piece: int = Field(
        ge=1,
        le=18,
        description="The floor piece the emission chains are counted under.",
    )
    watcher_max_age_seconds: int = Field(
        ge=1, description="A watcher state older than this is refused, never read."
    )
    watcher_state_path: str | None = Field(
        default=None, description="File only: the PR watcher's state."
    )
    facts_path: str | None = Field(
        default=None,
        description="File only: the landing facts file the snapshot step's facts source reads.",
    )
    emission_journal: str | None = Field(
        default=None,
        description="File only: the floor's emission journal each pass's chain is written to.",
    )

    @field_validator("repositories")
    @classmethod
    def _bare_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            if not name or "/" in name or "#" in name:
                raise ValueError(f"repository {name!r} is not a bare repository name")
        return value

    @model_validator(mode="after")
    def _file_needs_its_fields(self) -> ModelWatcherFactsOverlay:
        if not re.match(REPO_PATTERN, f"{self.owner}/x"):
            raise ValueError(f"owner {self.owner!r} cannot form a repository name")
        if self.backend is EnumWatcherFactsBackend.FILE:
            missing = [name for name in _FILE_FIELDS if not getattr(self, name)]
            if missing:
                raise ValueError(f"the file backend needs {', '.join(missing)}")
        return self


__all__: list[str] = [
    "OVERLAY_ID",
    "EnumWatcherFactsBackend",
    "ModelWatcherFactsOverlay",
]
