# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Composition-time read of the worker-engine config: the node's contract, then its overlay (OMN-21958).

The contract's ``config`` holds the Codex engine id and the default of each Codex route. The deployment's
``landing_worker_engine`` overlay may turn a route off under ``codex_routes``; a key the overlay does not
carry keeps the contract's value, and an unknown key or a non-boolean refuses naming ``codex_routes``.
The pure handler is handed the result and reads nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.nodes.node_landing_worker_engine_compute.models.model_worker_engine_facts import (
    ModelWorkerEngineConfig,
)

NODE = "node_landing_worker_engine_compute"
OVERLAY_ID = "landing_worker_engine"


class ModelCodexRoutes(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    mechanical: bool
    fallback: bool


class ModelCodexRoutesOverlay(BaseModel):
    """``<root>/landing_worker_engine/overlay.yaml``; ``codex_routes`` carries only the keys it changes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    codex_routes: dict[str, bool]


def config_from_section(section: object, source: str) -> ModelWorkerEngineConfig:
    if (
        not isinstance(section, dict)
        or "codex_engine" not in section
        or "codex_routes" not in section
    ):
        raise ValueError(
            f"{source}: config.codex_engine and config.codex_routes are required"
        )
    routes = ModelCodexRoutes.model_validate(section["codex_routes"])
    return ModelWorkerEngineConfig(
        codex_engine=section["codex_engine"],
        codex_mechanical=routes.mechanical,
        codex_fallback=routes.fallback,
    )


def load_worker_engine_config(
    environ: Mapping[str, str] | None = None,
) -> ModelWorkerEngineConfig:
    """The packaged contract's config with the overlay's ``codex_routes`` laid over it."""
    section: Any = load_packaged_contract_section(NODE, "config")
    base = config_from_section(section, f"{NODE}/contract.yaml")
    if find_node_overlay(OVERLAY_ID, environ) is None:
        return base
    overlay = load_structured_overlay(OVERLAY_ID, ModelCodexRoutesOverlay, environ)
    routes = {
        "mechanical": base.codex_mechanical,
        "fallback": base.codex_fallback,
        **overlay.codex_routes,
    }
    try:
        merged = ModelCodexRoutes.model_validate(routes)
    except ValidationError:
        unknown = sorted(set(overlay.codex_routes) - {"mechanical", "fallback"})
        raise NodeOverlayError(
            f"{OVERLAY_ID}.codex_routes carries unknown keys {unknown}; "
            f"the routes are mechanical and fallback"
        ) from None
    return base.model_copy(
        update={
            "codex_mechanical": merged.mechanical,
            "codex_fallback": merged.fallback,
        }
    )
