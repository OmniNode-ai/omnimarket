# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_local_environment_observe_effect: the services a lane runs.

Found at ``<root>/node_local_environment_observe_effect/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The models live apart from the handler on purpose: the private
overlays repository's check loads each overlay through its node's model with only pydantic and
pyyaml installed, and the handler module needs the runtime's packages (an HTTP client, the
infrastructure and core libraries). Nothing here may import beyond pydantic and the standard library.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelProbeTarget(BaseModel):
    """Service-network destination derived from a catalog manifest."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    service: str = Field(min_length=1)
    port: int = Field(ge=1, le=65535)
    scheme: Literal["http", "https"] | None = None
    path: str = "/"
    query: str = ""


class ModelLocalEnvironmentRoster(BaseModel):
    """The services a lane runs, as its overlay names them; probed in place of the catalog."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    services: tuple[ModelProbeTarget, ...] = Field(min_length=1)
