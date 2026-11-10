# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing tick orchestrator's deployment overlay: every setting, no code default for a deployment fact."""

from __future__ import annotations

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
    bootstrap_servers: str | None = Field(default=None, description="Redpanda only.")
    consumer_group: str | None = Field(default=None, description="Redpanda only.")
    snapshot_topic: str | None = Field(
        default=None,
        description="Redpanda only: the topic the landing snapshot step's tick records arrive on (its record_topic).",
    )
    poll_timeout_s: float | None = Field(
        default=None, gt=0, description="Redpanda only."
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
        return self


__all__: list[str] = ["OVERLAY_ID", "EnumTickBackend", "ModelTickOrchestratorOverlay"]
