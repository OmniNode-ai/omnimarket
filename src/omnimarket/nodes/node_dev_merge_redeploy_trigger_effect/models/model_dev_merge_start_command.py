# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The redeploy-start command this node publishes, as the producer's own typed model.

The consumer's model (``ModelRedeployStartCommand`` in the redeploy orchestrator node) forbids
extra fields, so this model carries exactly the fields a dev-merge trigger sets and leaves every
other field to the consumer's defaults. A join test validates this model's JSON through the
consumer's model, so the two cannot drift apart unseen. The shared deploy types come from
``omnimarket.events.runtime_deployment``.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.events.runtime_deployment import (
    EnumBuildSource,
    EnumRedeployScope,
    EnumRuntimeLane,
)


class ModelDevMergeStartCommand(BaseModel):
    """The dev-lane rebuild a dev merge asks the orchestrator for."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    scope: EnumRedeployScope
    git_ref: str = Field(
        min_length=1, description="The build-context commit the lane rebuilds at."
    )
    runtime_lane: EnumRuntimeLane
    build_source: EnumBuildSource
    requested_by: str = Field(min_length=1)
    requested_at: AwareDatetime


__all__ = ["ModelDevMergeStartCommand"]
