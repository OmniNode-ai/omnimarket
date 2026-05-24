# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from pydantic import BaseModel, ConfigDict


class ModelSwarmEndpoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    base_url: str
    health_check_path: str = "/health"
    model_id: str
    provider: str


__all__: list[str] = ["ModelSwarmEndpoint"]
