# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for node_epic_team_orchestrator [OMN-12206].

ModelEpicTeamRequest: carries the Linear epic ID and execution flags consumed
by the orchestrator when triggered via onex.cmd.omnimarket.epic-team-start.v1.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumEpicTeamMode(StrEnum):
    """Valid execution modes for the epic team orchestrator."""

    BUILD = "build"


class ModelEpicTeamRequest(BaseModel):
    """Input to the epic team orchestrator.

    All flags mirror the /epic-team skill surface defined in
    omniclaude/plugins/onex/skills/epic_team/SKILL.md.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    epic_id: str = Field(
        description="Linear epic ID to orchestrate (e.g. 'OMN-2000').",
    )
    mode: EnumEpicTeamMode = Field(
        default=EnumEpicTeamMode.BUILD,
        description="Execution mode. Only 'build' is valid for the epic team orchestrator.",
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "When true, print the decomposition plan (includes unmatched reason) "
            "and exit without spawning workers."
        ),
    )
    force: bool = Field(
        default=False,
        description=(
            "Archive any existing in-progress run state and restart from scratch. "
            "Pauses if active tasks remain unless force_kill is also set."
        ),
    )
    force_kill: bool = Field(
        default=False,
        description=(
            "Combined with force: destroy the active run even when live workers remain. "
            "Unsafe — intended for recovery from unrecoverable stalls only."
        ),
    )
    resume: bool = Field(
        default=False,
        description=(
            "Re-enter monitoring from the last persisted checkpoint. "
            "Finalises the epic if all tasks are already terminal; no-op if already done."
        ),
    )
    force_unmatched: bool = Field(
        default=False,
        description=(
            "Route tickets that do not match any repo in the repo_manifest to omniplan "
            "as TRIAGE tasks instead of skipping them."
        ),
    )
