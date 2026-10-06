# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed payloads for the hook-chain probe schedule trigger."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelHookChainProbeHeartbeat(BaseModel):
    """The runtime heartbeat as this node's input.

    ``extra="ignore"``: it is validated against the whole heartbeat payload and
    nothing in the schedule depends on its contents.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    service_name: str | None = None
    node_id: str | None = None


class ModelHookChainProbeScheduleRequest(BaseModel):
    """The command published to the probe; wire-identical to the probe's input."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str | None = Field(
        default=None, description="Minted by the probe when omitted."
    )
    timeout_seconds: float = Field(default=30.0, gt=0.0)


__all__ = ["ModelHookChainProbeHeartbeat", "ModelHookChainProbeScheduleRequest"]
