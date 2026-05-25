# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class ModelDispatchRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str
    command_type: str
    target_node_id: str
    payload: dict[str, Any]
    requested_by: str
    requested_at: str


class ModelDispatchResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    request_id: str
    status: str
    target_node_id: str
    error_message: str | None = None
    dispatched_at: str
