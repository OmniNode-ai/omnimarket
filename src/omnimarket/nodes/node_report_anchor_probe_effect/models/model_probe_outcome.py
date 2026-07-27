# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-claim probe-result models for the report anchor-probe EFFECT (OMN-15164)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_report_anchor_probe_effect.models.model_probe_status import (
    EnumAnchorProbeStatus,
)


class ModelShaProbeResult(BaseModel):
    """Resolution result for one ``*_sha`` claim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field_name: str
    sha: str
    status: EnumAnchorProbeStatus
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return self.status is EnumAnchorProbeStatus.RESOLVED


class ModelPathProbeResult(BaseModel):
    """Existence + containment result for one ``*_paths`` claim entry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field_name: str
    path: str
    resolved_path: str = Field(
        default="", description="Absolute path after resolve(); '' if never resolved."
    )
    status: EnumAnchorProbeStatus
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return self.status is EnumAnchorProbeStatus.RESOLVED


class ModelPrProbeResult(BaseModel):
    """Confirmation result for the optional PR-number claim."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field_name: str
    pr_number: int
    repo: str
    status: EnumAnchorProbeStatus
    state: str = Field(
        default="",
        description="gh-reported PR state (OPEN/MERGED/CLOSED), if resolved.",
    )
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return self.status is EnumAnchorProbeStatus.RESOLVED


__all__ = [
    "ModelPathProbeResult",
    "ModelPrProbeResult",
    "ModelShaProbeResult",
]
