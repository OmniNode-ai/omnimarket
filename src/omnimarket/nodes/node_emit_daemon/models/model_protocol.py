# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Socket protocol request/response models for the emit daemon.

Protocol: newline-delimited JSON over Unix socket.
"""

from __future__ import annotations

import logging
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# Type alias for JSON-serializable values
JsonType = dict[str, object] | list[object] | str | int | float | bool | None

# =============================================================================
# Request Models
# =============================================================================


class ModelDaemonPingRequest(BaseModel):
    """Ping/health check request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: Literal["ping"] = Field(default="ping")


class ModelDaemonHealthRequest(BaseModel):
    """Detailed health check request (returns circuit breaker state)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: Literal["health"] = Field(default="health")


class ModelDaemonEmitRequest(BaseModel):
    """Event emission request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_type: str = Field(..., min_length=1)
    payload: JsonType = Field(default_factory=dict)


ModelDaemonRequest = Annotated[
    ModelDaemonPingRequest | ModelDaemonHealthRequest | ModelDaemonEmitRequest,
    Field(description="Union of all daemon request types"),
]


def parse_daemon_request(
    data: dict[str, object],
) -> ModelDaemonPingRequest | ModelDaemonHealthRequest | ModelDaemonEmitRequest:
    """Parse raw dict into typed request model.

    Discriminates by field presence:
    - "command": "ping" -> ModelDaemonPingRequest
    - "command": "health" -> ModelDaemonHealthRequest
    - "event_type" -> ModelDaemonEmitRequest
    """
    if "command" in data and "event_type" in data:
        raise ValueError(
            "Ambiguous request: contains both 'command' and 'event_type' fields. "
            "Send separate requests for ping and emit operations."
        )
    if "command" in data:
        cmd = data["command"]
        if cmd == "health":
            return ModelDaemonHealthRequest.model_validate(data)
        return ModelDaemonPingRequest.model_validate(data)
    if "event_type" in data:
        return ModelDaemonEmitRequest.model_validate(data)
    raise ValueError(
        "Invalid request: must contain either 'command' or 'event_type' field"
    )


# =============================================================================
# Response Models
# =============================================================================


class ModelDaemonPingResponse(BaseModel):
    """Ping response with queue status."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["ok"] = Field(default="ok")
    queue_size: int = Field(..., ge=0)
    spool_size: int = Field(..., ge=0)


class ModelDaemonQueuedResponse(BaseModel):
    """Event successfully queued response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["queued"] = Field(default="queued")
    event_id: str = Field(..., min_length=1)


class ModelDaemonErrorResponse(BaseModel):
    """Error response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["error"] = Field(default="error")
    reason: str = Field(..., min_length=1)


ModelDaemonResponse = Annotated[
    ModelDaemonPingResponse | ModelDaemonQueuedResponse | ModelDaemonErrorResponse,
    Field(discriminator="status"),
]


def parse_daemon_response(
    data: dict[str, object],
) -> ModelDaemonPingResponse | ModelDaemonQueuedResponse | ModelDaemonErrorResponse:
    """Parse raw dict into typed response model."""
    status = data.get("status")
    if status == "ok":
        return ModelDaemonPingResponse.model_validate(data)
    if status == "queued":
        return ModelDaemonQueuedResponse.model_validate(data)
    if status == "error":
        return ModelDaemonErrorResponse.model_validate(data)
    raise ValueError(f"Unknown response status: {status}")


__all__: list[str] = [
    "JsonType",
    "ModelDaemonEmitRequest",
    "ModelDaemonErrorResponse",
    "ModelDaemonHealthRequest",
    "ModelDaemonPingRequest",
    "ModelDaemonPingResponse",
    "ModelDaemonQueuedResponse",
    "ModelDaemonRequest",
    "ModelDaemonResponse",
    "parse_daemon_request",
    "parse_daemon_response",
]
