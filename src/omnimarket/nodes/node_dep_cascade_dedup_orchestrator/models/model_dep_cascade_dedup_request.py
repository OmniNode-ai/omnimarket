# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input model for node_dep_cascade_dedup_orchestrator [OMN-12213].

ModelDepCascadeDedupRequest: carries the repos to scan, optional dependency-type
filter, label filter, dry-run flag, and close comment override consumed by the
orchestrator when triggered via onex.cmd.omnimarket.dep-cascade-dedup-start.v1.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelDepCascadeDedupRequest(BaseModel):
    """Input to the dep cascade dedup orchestrator.

    All flags mirror the /dep-cascade-dedup skill surface defined in
    omniclaude/plugins/onex/skills/dep_cascade_dedup/SKILL.md.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    repos: tuple[str, ...] = Field(
        default=(),
        description=(
            "Repos to scan, in 'owner/name' or bare 'name' form. "
            "When empty, all OmniNode-ai repos are discovered via "
            "`gh repo list OmniNode-ai --json name`."
        ),
    )
    dependency_type: str = Field(
        default="",
        description=(
            "Optional filter on the dependency type label (e.g. 'python', 'npm'). "
            "Empty string means no filter beyond the label match."
        ),
    )
    label: str = Field(
        default="dependencies",
        description="PR label used to identify dep-bump PRs.",
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "When true, identify superseded PRs and report them without closing. "
            "No GitHub mutations are performed."
        ),
    )
    close_comment: str = Field(
        default="",
        description=(
            "Comment posted on each closed PR. When empty the orchestrator "
            "generates: 'Superseded by #{keeper} targeting {package}@{version}. "
            "Closed by dep-cascade-dedup [OMN-6740].'."
        ),
    )
