# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Configuration model for the architecture graph populate handler."""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelArchitectureGraphPopulateConfig"]


def _bolt_uri_from_env() -> str:
    """Resolve Bolt URI from env — fail fast if not set."""
    return os.environ["ARCH_GRAPH_BOLT_URI"]


class ModelArchitectureGraphPopulateConfig(BaseModel):
    """Configuration for HandlerArchitectureGraphPopulate.

    All connection parameters come from env vars resolved at instantiation time.
    The ARCH_GRAPH_BOLT_URI env var is required; initialization will raise
    KeyError if it is absent rather than silently falling back to localhost.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph_backend: str = Field(
        default="memgraph",
        description="Graph backend identifier (contract config: graph_backend)",
    )
    bolt_uri: str = Field(
        default_factory=_bolt_uri_from_env,
        description="Bolt protocol URI for the graph database (from ARCH_GRAPH_BOLT_URI)",
    )
    timeout_seconds: float = Field(
        default=60.0,
        ge=1.0,
        le=300.0,
        description="Maximum time for graph populate operations in seconds",
    )
    graph_schema_version: str = Field(
        default="1.0.0",
        description="Schema version stamped on every snapshot for compatibility tracking",
    )
    batch_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Number of MERGE statements per transaction batch",
    )
