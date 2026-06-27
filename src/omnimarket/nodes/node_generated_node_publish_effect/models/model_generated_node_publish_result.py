# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelGeneratedNodePublishResult -- output contract for the publish effect.

SEA Phase 7.2 (OMN-13625): adds ``entry_point_registered`` to record whether the
node was successfully added to pyproject.toml's [project.entry-points."onex.nodes"]
section during the publish run.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelGeneratedNodePublishResult(BaseModel):
    """Output from the generated-node publish effect node."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Correlation ID from the input.")
    node_name: str = Field(..., description="Name of the published node package.")
    repo: str = Field(..., description="Target GitHub repo slug (org/repo).")
    published: bool = Field(
        ..., description="True when a PR was successfully opened for the package."
    )
    pr_url: str | None = Field(
        default=None,
        description="URL of the opened PR, or None when publish was blocked.",
    )
    branch: str | None = Field(
        default=None,
        description="Branch the package was committed to, or None when blocked.",
    )
    blocked_reason: str | None = Field(
        default=None,
        description="Human-readable reason publish was blocked, or None on success.",
    )
    entry_point_registered: bool = Field(
        default=False,
        description=(
            "True when the node was successfully added to pyproject.toml's "
            '[project.entry-points."onex.nodes"] section during this publish run '
            "(or was already present). False when registration was skipped "
            "(register_entry_point=False) or publish was blocked before registration "
            "could complete. (SEA Phase 7.2, OMN-13625)"
        ),
    )


__all__: list[str] = ["ModelGeneratedNodePublishResult"]
