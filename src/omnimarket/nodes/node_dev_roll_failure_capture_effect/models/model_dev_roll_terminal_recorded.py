# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The roll's typed terminal record, published on success (OMN-24013)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_dev_roll_failure_capture_effect.models.enum_dev_roll_outcome import (
    EnumDevRollOutcome,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.models.model_dev_roll_log_artifact import (
    ModelDevRollLogArtifact,
)


class ModelDevRollTerminalRecorded(BaseModel):
    """The terminal record the incident capture writes; one per incident."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    issuer: str
    commit: str
    outcome: EnumDevRollOutcome
    restored_revision: str
    error: str
    recovered_at: datetime
    capture_complete: bool
    log_artifacts: tuple[ModelDevRollLogArtifact, ...]
    health_probe_services: tuple[str, ...]
    record_ref: str
