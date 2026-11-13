# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed roll terminal published by the incident handler."""

from __future__ import annotations

from datetime import datetime
from uuid import NAMESPACE_DNS, UUID, uuid5

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.nodes.node_dev_roll_failure_capture_effect.models.enum_dev_roll_outcome import (
    EnumDevRollOutcome,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.models.enum_dev_roll_unknown_reason import (
    EnumDevRollUnknownReason,
)
from omnimarket.nodes.node_dev_roll_failure_capture_effect.models.model_dev_roll_log_artifact import (
    ModelDevRollLogArtifact,
)

_TERMINAL_NAMESPACE = uuid5(NAMESPACE_DNS, "omnimarket.dev-roll-terminal")


class ModelDevRollTerminal(BaseModel):
    """One typed terminal per incident."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    terminal_id: UUID
    job_id: str | None
    issuer: str | None
    outcome: EnumDevRollOutcome
    unknown_reason: EnumDevRollUnknownReason | None
    detail: str | None
    observed_at: datetime
    incident_at: str | None
    commit: str | None
    error: str | None
    recovery_verified: bool | None
    manifest_seen: bool
    manifest_issuer_matches: bool | None
    health_probe_count: int
    log_artifacts: tuple[ModelDevRollLogArtifact, ...]
    terminal_artifact_key: str | None

    @model_validator(mode="after")
    def _check_unknown_reason(self) -> ModelDevRollTerminal:
        if self.outcome == EnumDevRollOutcome.UNKNOWN and self.unknown_reason is None:
            raise ValueError("unknown_reason is required when outcome is UNKNOWN")
        if (
            self.outcome != EnumDevRollOutcome.UNKNOWN
            and self.unknown_reason is not None
        ):
            raise ValueError("unknown_reason must be None when outcome is not UNKNOWN")
        return self

    @staticmethod
    def make_terminal_id(job_id: str | None) -> UUID:
        return uuid5(_TERMINAL_NAMESPACE, job_id or "")
