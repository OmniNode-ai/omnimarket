# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing tick orchestrator's deployment overlay: every setting, no code default for a deployment fact."""

from __future__ import annotations

import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.events.pr_landing_github.enum_pr_landing_github_mode import (
    EnumPrLandingGithubMode,
)
from omnimarket.models.landing_decision import REPO_PATTERN

OVERLAY_ID = "node_pr_landing_tick_orchestrator"


class EnumTickBackend(StrEnum):
    INMEMORY = "inmemory"
    REDPANDA = "redpanda"


class ModelTickOrchestratorOverlay(BaseModel):
    """<root>/node_pr_landing_tick_orchestrator/overlay.yaml."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: EnumTickBackend = Field(
        description="Where recorded tick snapshots are read from."
    )
    act_enabled: bool = Field(
        description="Whether act mode may call the writer. False while the script keeps landing."
    )
    cutover_repository: str | None = Field(
        default=None,
        pattern=REPO_PATTERN,
        description=(
            "The one repository the act-mode writer lands (owner/name); every other "
            "repository's action is refused as out_of_cutover. Absent until the cutover "
            "rule reads true (OMN-23405)."
        ),
    )
    github_mode: EnumPrLandingGithubMode | None = Field(
        default=None,
        description=(
            "The GitHub landing effect's mode the act-mode writer sends: dry_run records, "
            "enforce writes. Required with act_enabled (OMN-23405)."
        ),
    )
    max_snapshots: int = Field(ge=1, description="Snapshots compared in one run.")
    floor_piece: int = Field(
        ge=1,
        le=18,
        description="The floor piece the emission chains are counted under.",
    )
    emission_journal: str | None = Field(
        default=None, description="Redpanda profile only: the emission journal path."
    )
    bootstrap_servers: str | None = Field(
        default=None,
        description="Redpanda only, in place of bus_lane: a plaintext lab broker started for a proof.",
    )
    bus_lane: str | None = Field(
        default=None,
        description=(
            "Redpanda only: the workspace bus lane the tick records are read from (dev); the broker "
            "and client transport resolve from the lane config under workspace_root (OMN-23458)."
        ),
    )
    workspace_root: str | None = Field(
        default=None,
        description="Redpanda with bus_lane: the workspace root whose lane config names the broker.",
    )
    consumer_group: str | None = Field(default=None, description="Redpanda only.")
    snapshot_topic: str | None = Field(
        default=None,
        description="Redpanda only: the topic the landing snapshot step's tick records arrive on (its record_topic).",
    )
    poll_timeout_s: float | None = Field(
        default=None, gt=0, description="Redpanda only."
    )
    script_out_of_scope_repositories: tuple[str, ...] = Field(
        default=(),
        description=(
            "The repositories the old landing controller rules out of scope (the merge sweep's "
            "out_of_scope list): it reads no facts and records no decision for them, so its recorded "
            "decision for such a repository is no action. Act mode writes that record for the "
            "cutover repository when it is one of these and the tick carries none (OMN-23458)."
        ),
    )

    @model_validator(mode="after")
    def _act_needs_a_cutover(self) -> ModelTickOrchestratorOverlay:
        if self.act_enabled and (
            self.cutover_repository is None or self.github_mode is None
        ):
            raise ValueError(
                "act_enabled needs cutover_repository and github_mode: act mode "
                "writes for one named repository only"
            )
        if self.bus_lane and self.bootstrap_servers:
            raise ValueError("name one of bus_lane or bootstrap_servers, not both")
        if self.bus_lane and not self.workspace_root:
            raise ValueError("bus_lane needs workspace_root")
        for repository in self.script_out_of_scope_repositories:
            if not re.fullmatch(REPO_PATTERN, repository):
                raise ValueError(
                    f"script_out_of_scope_repositories entry {repository!r} is not owner/name"
                )
        return self


class ModelTickTopicSelection(BaseModel):
    """Which topic the snapshot reader reads, under which group, from which broker (OMN-23458).

    The broker is a declared bus lane under a workspace root (the deployed form), or a plaintext
    lab broker started for a proof; never both.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(min_length=1)
    consumer_group: str = Field(min_length=1)
    bus_lane: str | None = None
    workspace_root: str | None = None
    bootstrap_servers: str | None = None

    @model_validator(mode="after")
    def _one_broker(self) -> ModelTickTopicSelection:
        if bool(self.bus_lane) == bool(self.bootstrap_servers):
            raise ValueError(
                "a topic selection names one of bus_lane or bootstrap_servers"
            )
        if self.bus_lane and not self.workspace_root:
            raise ValueError("bus_lane needs workspace_root")
        return self


__all__: list[str] = [
    "OVERLAY_ID",
    "EnumTickBackend",
    "ModelTickOrchestratorOverlay",
    "ModelTickTopicSelection",
]
