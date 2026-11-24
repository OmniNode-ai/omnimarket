# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the gateway-graded delegation checks (OMN-26642): one observation and the route a check resolves to."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ModelGatewayObservation(BaseModel):
    """What one gateway request showed. ``error`` is set, and ``status`` is None, when no HTTP answer was obtained."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: int | None
    body: Any = None
    error: str | None = None


class ModelCheckRoute(BaseModel):
    """Where a check is graded: the endpoint it talks to, the runner that runs it and the board that reads it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_id: str
    kind: Literal["gateway", "gateway-and-customer-machine"]
    endpoint: str
    runner: str
    board: str
