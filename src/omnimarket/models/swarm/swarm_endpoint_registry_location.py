# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the swarm endpoint registry is read from.

The registry names the model endpoints a deployment runs, so it is deployment
data: the package ships the schema (``ModelRegistryEndpoint``) and this
resolver, never an endpoint. The registry is the ``overlay.yaml`` of
``node_swarm_registry_compute`` (a ``registry_schema_version`` plus an
``endpoints`` list), found through ``ONEX_SKILL_OVERLAY_ROOTS`` or the explicit
``OMNIMARKET_SWARM_ENDPOINT_REGISTRY`` file pointer. With neither, resolution
refuses and says how to supply one.
"""

from __future__ import annotations

from pathlib import Path

from omnimarket.models.node_overlay.node_overlay_reader import (
    NodeOverlayError,
    find_node_overlay,
    overlay_hint,
)

SWARM_REGISTRY_NODE = "node_swarm_registry_compute"
SWARM_REGISTRY_POINTER_ENV = "OMNIMARKET_SWARM_ENDPOINT_REGISTRY"


class SwarmRegistryNotConfiguredError(ValueError):
    """No swarm endpoint registry was supplied by the deployment."""


def resolve_endpoint_registry_path(explicit: Path | None = None) -> Path:
    """Return the registry file: ``explicit``, else the pointer, else the overlay roots."""
    if explicit is not None:
        return explicit
    try:
        found = find_node_overlay(SWARM_REGISTRY_NODE, SWARM_REGISTRY_POINTER_ENV)
    except NodeOverlayError as exc:
        raise SwarmRegistryNotConfiguredError(str(exc)) from None
    if found is None:
        raise SwarmRegistryNotConfiguredError(
            "no swarm endpoint registry is configured: "
            + overlay_hint(SWARM_REGISTRY_NODE, SWARM_REGISTRY_POINTER_ENV)
        )
    return found


__all__ = [
    "SWARM_REGISTRY_NODE",
    "SWARM_REGISTRY_POINTER_ENV",
    "SwarmRegistryNotConfiguredError",
    "resolve_endpoint_registry_path",
]
