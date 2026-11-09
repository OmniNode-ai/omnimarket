# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and response of the plan catalog read handler."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class ModelPlanListRequest(BaseModel):
    """GET /v1/plans carries no input."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelPlanListResponse(BaseModel):
    """Status code and JSON body the mount answers with."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status_code: int
    body: Any


__all__: list[str] = ["ModelPlanListRequest", "ModelPlanListResponse"]
