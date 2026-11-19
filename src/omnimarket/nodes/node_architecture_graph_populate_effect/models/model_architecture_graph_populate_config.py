# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Configuration model for the architecture graph populate handler."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.arch_graph_bolt_host_overlay import resolve_arch_graph_bolt_uri

__all__ = ["ModelArchitectureGraphPopulateConfig"]

# The Bolt URI resolves through ``resolve_arch_graph_bolt_uri``: the contract reference
# ``${env.ARCH_GRAPH_BOLT_URI}`` through the sanctioned overlay boundary, then this host's
# row of the ``arch_graph_bolt`` overlay. The handler must NOT read ``os.environ`` directly
# (OMN-13557 endpoint-overlay migration).


def _bolt_uri_from_overlay() -> str:
    """Resolve the Bolt URI through the overlay seam — fail closed if unset.

    The variable ``ARCH_GRAPH_BOLT_URI`` wins where a lane sets it; a host that sets
    nothing reads its own row of the ``arch_graph_bolt`` overlay (OMN-24687). With
    neither, resolution raises rather than silently falling back to localhost.
    """
    return resolve_arch_graph_bolt_uri()


class ModelArchitectureGraphPopulateConfig(BaseModel):
    """Configuration for HandlerArchitectureGraphPopulate.

    Connection parameters resolve from the contract + per-lane overlay through
    the sanctioned overlay boundary. The ``bolt_uri`` field binds the contract
    reference ``${env.ARCH_GRAPH_BOLT_URI}``; if the active overlay does not
    bind it, initialization fails closed with a ``ValueError`` rather than
    silently falling back to localhost.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    graph_backend: str = Field(
        default="memgraph",
        description="Graph backend identifier (contract config: graph_backend)",
    )
    bolt_uri: str = Field(
        default_factory=_bolt_uri_from_overlay,
        description=(
            "Bolt protocol URI for the graph database "
            "(contract ${env.ARCH_GRAPH_BOLT_URI}, resolved via overlay)"
        ),
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
