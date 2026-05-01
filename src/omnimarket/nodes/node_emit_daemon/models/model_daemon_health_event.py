# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Typed payload model for portable daemon health diagnostic events."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelDaemonHealthEvent(BaseModel):
    """Payload published on onex.evt.diagnostic.daemon-health.v1."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    daemon_id: str = Field(..., min_length=1)
    pid: int = Field(..., ge=1)
    socket_path: str = Field(..., min_length=1)
    kafka_offset: int = Field(..., ge=0)
    round_trip_ms: float = Field(..., ge=0.0)
    status: Literal["PASS", "WARN", "FAIL"]


__all__: list[str] = ["ModelDaemonHealthEvent"]
