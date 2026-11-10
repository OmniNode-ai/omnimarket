# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One HTTP route the node's contract declares for its runtime mount."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_inference_credentials.models.model_inference_credentials_http import (
    EnumInferenceCredentialsOperation,
)


class ModelInferenceCredentialsRoute(BaseModel):
    """Operation, method and path of one declared route (the path holds the mount prefix)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: EnumInferenceCredentialsOperation
    method: Literal["POST", "GET", "DELETE"]
    path: str = Field(..., pattern=r"^/[A-Za-z0-9._~/{}\-]+$")
    authorization: Literal["tenant"]


__all__: list[str] = ["ModelInferenceCredentialsRoute"]
