# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and result for the remote-lane runner's pre-placement brief and lane-shape refusals."""

from dataclasses import dataclass
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class ModelBriefDestinationPolicy(BaseModel):
    """Deployment authority for repositories whose code must move before editing."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    frozen_repositories: tuple[
        Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")], ...
    ] = Field(min_length=1)
    migration_repository: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class ModelBriefDestinationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    authored_prose: str
    repository: str = ""


class ModelBriefDestinationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    refusals: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ModelDispatchBriefShapeRequest:
    brief: str
    lane: str
    prs: tuple[str, ...]
    lane_brief_path: str  # lane-brief's lane_brief.py, whose prose readers and mint rule this node applies
    ticket: str | None = (
        None  # the dispatch's --ticket; the brief's own CLAIM ticket is read from the brief
    )
    repo: str | None = None  # the dispatch's --repo; a lane with a repo may open a PR


@dataclass(frozen=True)
class ModelDispatchBriefShapeResult:
    refusals: tuple[
        tuple[str, str], ...
    ]  # (receipt status, REMOTE-LANE-* line); the first one refuses the lane


class ModelContextPackCheckRequest(BaseModel):
    """A brief and the facts the context pack check judges it from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    brief: str
    prose: (
        str  # the brief's own prose, with fenced blocks and the pasted rules left out
    )
    # Ledger rows that may confirm the approval stamp. None: no reader was given.
    ledger_rows: tuple[str, ...] | None = None
    # The reader's own failure, if any; a read that failed is a refusal, never a pass.
    ledger_error: str = ""
    # Words in a lane name that make its brief open-ended (no code work, so no pack is asked).
    open_ended_words: tuple[str, ...] = (
        "diagnose",
        "diagnosis",
        "cause",
        "plan",
        "investigate",
        "investigation",
    )


class ModelContextPackCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    problems: tuple[str, ...]
