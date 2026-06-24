# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Configuration model for the architecture graph query handler."""

from __future__ import annotations

from omnibase_infra.runtime.overlay.contract_env_ref import expand_contract_env_refs
from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelArchitectureGraphQueryConfig"]

# Contract-declared endpoint reference. The Bolt URI for the Memgraph instance
# resolves through the sanctioned overlay boundary (``expand_contract_env_refs``,
# the one env-reading surface) — a per-lane overlay binds ``ARCH_GRAPH_BOLT_URI``
# to the dev/stability/prod value. The handler must NOT read ``os.environ``
# directly (OMN-13557 endpoint-overlay migration).
_BOLT_URI_CONTRACT_REF = "${env.ARCH_GRAPH_BOLT_URI}"


def _bolt_uri_from_overlay() -> str:
    """Resolve the Bolt URI through the overlay seam — fail closed if unset.

    The contract declares ``bolt_uri`` as ``${env.ARCH_GRAPH_BOLT_URI}``; the
    overlay resolver expands it against the lane environment. An unset var
    expands to the empty string, which fails the explicit guard below rather
    than silently falling back to localhost.
    """
    resolved: str = expand_contract_env_refs(_BOLT_URI_CONTRACT_REF)
    if not resolved:
        raise ValueError(
            "ARCH_GRAPH_BOLT_URI is not bound by the active overlay; declare it "
            "in the per-lane overlay so the contract reference "
            f"{_BOLT_URI_CONTRACT_REF!r} resolves to a Bolt URI."
        )
    return resolved


class ModelArchitectureGraphQueryConfig(BaseModel):
    """Configuration for HandlerArchitectureGraphQuery.

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
