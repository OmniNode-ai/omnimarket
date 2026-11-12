# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed bus-model seam check request."""

from pydantic import BaseModel, ConfigDict


class ModelBusModelSeamCheckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    repo_root: str
    package: str = "omnimarket"
    policy_path: str | None = None
