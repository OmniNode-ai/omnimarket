# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Caller-supplied draw evidence; the compute handler performs no file reads."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class ModelRouteCheckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    operation: Literal["delegation_route_check"] = "delegation_route_check"
    receipt: dict[str, JsonValue]
    run_directory: str = Field(min_length=1)
    route: Literal["L", "C"]
    pin: str = Field(min_length=1)
    lane: str | None = None
    secret_ref: str | None = None
    tenant: str | None = None
    model: str | None = None
