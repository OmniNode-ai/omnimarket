# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The check's verdict: the findings, the pilot's status and any refused extension."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_pilot_manifest_check_compute.models.enum_pilot_check_finding import (
    EnumPilotCheckFinding,
)
from omnimarket.nodes.node_pilot_manifest_check_compute.models.enum_pilot_status import (
    EnumPilotStatus,
)


class ModelPilotManifestCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    pilot: str
    check_passed: bool
    findings: tuple[EnumPilotCheckFinding, ...] = ()
    computed_bar_hash: str | None = None
    status: EnumPilotStatus
    failed_metrics: tuple[str, ...] = ()
    extension_refused: bool = False
