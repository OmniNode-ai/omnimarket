# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and response of the usage privilege catalog read handler."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelUsagePrivilegesRequest(BaseModel):
    """The request headers the identity resolver reads; the route has no other input."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    headers: dict[str, str] = Field(default_factory=dict)


class ModelUsagePrivilegesResponse(BaseModel):
    """Status code and JSON body the mount answers with."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status_code: int
    body: Any


__all__: list[str] = ["ModelUsagePrivilegesRequest", "ModelUsagePrivilegesResponse"]
