# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Configuration model for the architecture graph query handler."""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelArchitectureGraphQueryConfig"]


def _bolt_uri_from_env() -> str:
    """Resolve Bolt URI from env — fail fast if not set."""
    return os.environ["ARCH_GRAPH_BOLT_URI"]


class ModelArchitectureGraphQueryConfig(BaseModel):
    """Configuration for HandlerArchitectureGraphQuery.

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
        default=30.0,
        ge=1.0,
        le=120.0,
        description="Maximum time for graph query operations in seconds",
    )
    max_path_depth: int = Field(
        default=10,
        ge=1,
        le=50,
        description="Maximum traversal depth for path and blast-radius queries",
    )
