# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for node_runner_orchestrator [OMN-12218].

Mirrors the runner skill's supported actions (deploy / update / status)
and the flags documented in omniclaude/plugins/onex/skills/runner/SKILL.md.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumRunnerAction(StrEnum):
    """Action to perform on the self-hosted runner fleet."""

    DEPLOY = "deploy"
    """Deploy or update runners on the CI host using the cached Docker image."""

    UPDATE = "update"
    """Force Docker image rebuild then redeploy (equivalent to deploy --rebuild)."""

    STATUS = "status"
    """Query GitHub API and SSH-inspect containers; emit per-runner health table."""


class ModelRunnerRequest(BaseModel):
    """Input envelope for the runner orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action: EnumRunnerAction = Field(
        description=(
            "Action to perform: deploy (use cached image), update (force rebuild), "
            "or status (health check)."
        ),
    )
    runner_name: str | None = Field(
        default=None,
        description=(
            "Target a specific runner by name (e.g. 'omninode-runner-9'). "
            "When None, the action applies to all runners discovered via the GitHub API."
        ),
    )
    config: ModelRunnerConfig | None = Field(
        default=None,
        description=(
            "Optional deploy/update configuration overrides. "
            "When None, defaults from the runner skill are used."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "When True, show compose diff and version comparison without making changes. "
            "Applies to deploy and update actions only."
        ),
    )
    correlation_id: str | None = Field(
        default=None,
        description="Upstream correlation ID for event tracing.",
    )


class ModelRunnerConfig(BaseModel):
    """Optional configuration overrides for deploy/update operations."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ci_host: str | None = Field(
        default=None,
        description=(
            "CI host to deploy to. Resolved from contract config when None. "
            "Must be annotated # onex-allow-internal-ip at the call site."
        ),
    )
    runner_version: str | None = Field(
        default=None,
        description=(
            "Pin a specific GitHub Actions runner binary version (e.g. '2.331.0'). "
            "When None, the version in the Dockerfile ARG is used."
        ),
    )
    compose_file: str | None = Field(
        default=None,
        description=(
            "Path to the runner docker-compose file on the CI host. "
            "Defaults to '~/.omnibase/runners/docker/docker-compose.runners.yml'."
        ),
    )
