# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and failure models of the host announcer (OMN-24367)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_host_introspection_compute.models.model_host_announcement import (
    EnumAnnounceReason,
)


class ModelHostFactsProbeRequest(BaseModel):
    """What the facts reader is asked for: this host, and what the process knows about it that no probe can read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str
    roles: list[str] = Field(default_factory=list)
    models_served: list[str] = Field(default_factory=list)
    takes_work: bool = True
    availability_reason: str = ""
    software: list[str] = Field(default_factory=list)


class ModelHostIntrospectionFailure(BaseModel):
    """A host whose facts could not be read says so, here, and publishes no announcement."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str
    reason: EnumAnnounceReason
    failed_at: datetime
    error_message: str = Field(default="", max_length=2000)
