# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for node_wave_scheduler_orchestrator [OMN-12210].

ModelWaveSchedulerRequest: carries the plan file path, concurrency cap, and
execution flags consumed by the orchestrator when triggered via
onex.cmd.omnimarket.wave-scheduler-start.v1.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelHealthcheckConfig(BaseModel):
    """Health-check policy for stall detection during wave execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = Field(
        default=True,
        description="Enable stall detection via agent_healthcheck.",
    )
    stall_timeout_seconds: int = Field(
        default=300,
        description="Seconds of worker inactivity before a stall is declared.",
    )
    max_recovery_attempts: int = Field(
        default=3,
        description="Maximum health-check recovery relaunches per ticket before marking failed.",
    )
    poll_interval_seconds: int = Field(
        default=30,
        description="Polling interval in seconds between health checks.",
    )


class ModelWaveSchedulerRequest(BaseModel):
    """Input to the wave scheduler orchestrator.

    All flags mirror the /wave-scheduler skill surface defined in
    omniclaude/plugins/onex/skills/wave_scheduler/SKILL.md.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_path: str = Field(
        description=(
            "Path to the plan YAML file containing ticket definitions with "
            "`depends_on` fields. Supports absolute paths and paths relative "
            "to OMNI_HOME."
        ),
    )
    max_concurrency: int = Field(
        default=6,
        ge=1,
        le=20,
        description="Maximum number of parallel ticket-pipeline agents per wave.",
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "Compute waves and log the dispatch plan without executing. "
            "Returns the computed wave schedule in the output."
        ),
    )
    resume: bool = Field(
        default=False,
        description=(
            "Resume from persisted wave state under "
            ".onex_state/wave_scheduler/{epic_id}/state.yaml. "
            "Skips completed waves; re-dispatches in-progress wave tickets."
        ),
    )
    fail_fast: bool = Field(
        default=False,
        description="Abort the entire execution on the first ticket failure.",
    )
    defer_repo_conflicts: bool = Field(
        default=False,
        description=(
            "When true, defer same-repo tickets within a wave to avoid "
            "worktree conflicts. Default false: each ticket gets its own worktree."
        ),
    )
    healthcheck_config: ModelHealthcheckConfig = Field(
        default_factory=ModelHealthcheckConfig,
        description="Health-check policy for stall detection during wave execution.",
    )
