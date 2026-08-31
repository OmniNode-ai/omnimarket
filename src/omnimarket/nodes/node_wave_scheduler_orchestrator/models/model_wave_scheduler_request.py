# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for node_wave_scheduler_orchestrator [OMN-12210, OMN-17017].

ModelWaveSchedulerRequest: carries the plan file path, concurrency cap, and
execution flags consumed by the orchestrator when triggered via
onex.cmd.omnimarket.wave-scheduler-start.v1.

OMN-17017 deleted ``healthcheck_config``: it was a four-field model with zero
handler references, omitted from the CLI as "not a supported CLI scalar arg
type" — unreachable AND inert. Stall detection belongs to
node_dispatch_watchdog_orchestrator, which owns it for real.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


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
            "Resume from the persisted checkpoint under "
            "<state_dir>/wave_scheduler/<run_id>/checkpoint.json. Tickets already "
            "observed COMPLETED are not re-dispatched. When no checkpoint exists "
            "the run starts clean and reports resumed=False."
        ),
    )
    fail_fast: bool = Field(
        default=False,
        description=(
            "Abort the entire execution on the first wave that reports any "
            "non-COMPLETED ticket. Undispatched tickets are reported SKIPPED and "
            "the run status is ABORTED."
        ),
    )
    defer_repo_conflicts: bool = Field(
        default=False,
        description=(
            "When true, defer same-repo tickets within a wave to avoid "
            "worktree conflicts. Default false: each ticket gets its own worktree."
        ),
    )
    state_dir: str | None = Field(
        default=None,
        description=(
            "Durable state root for dispatch-lifecycle records and checkpoints. "
            "When unset it resolves from $ONEX_STATE_DIR, else $OMNI_HOME/.onex_state."
        ),
    )
