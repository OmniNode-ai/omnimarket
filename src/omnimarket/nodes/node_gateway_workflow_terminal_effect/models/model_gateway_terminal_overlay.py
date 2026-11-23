# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deployment document ``node_gateway_workflow_terminal_effect/overlay.yaml``."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelGatewayTerminalDatabase(BaseModel):
    """How the writer reaches the gateway's Postgres store (the same logical secret as the gateway)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    secret_resolver_config_path: str = Field(..., min_length=1)
    dsn_secret: str = Field(..., min_length=1, description="Logical secret name.")
    pool_max_size: int = Field(default=2, gt=0)
    query_timeout_seconds: float = Field(default=10.0, gt=0)


class ModelGatewayTerminalOverlay(BaseModel):
    """Every deployment value of the terminal writer; nothing in it is a secret."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    database: ModelGatewayTerminalDatabase
    broker_bootstrap_servers_env: str = Field(
        default="KAFKA_BOOTSTRAP_SERVERS", min_length=1
    )
    publish_timeout_seconds: float = Field(default=10.0, gt=0)


__all__: list[str] = ["ModelGatewayTerminalDatabase", "ModelGatewayTerminalOverlay"]
