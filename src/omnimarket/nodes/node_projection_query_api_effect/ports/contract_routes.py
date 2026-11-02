# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the node's own contract: the routes it serves and the mount that carries them (OMN-21288).

The contract is the only place a path, a query parameter or a topic of this node
is written. The handler and the ASGI app read it here.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

from omnimarket.nodes.node_projection_query_api_effect.models.model_query_route import (
    ModelQueryMount,
    ModelQueryRoute,
)

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


@lru_cache(maxsize=1)
def _contract() -> dict[str, object]:
    raw = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{_CONTRACT_PATH} must hold a mapping")
    return raw


def contract_routes() -> tuple[ModelQueryRoute, ...]:
    """The routes the contract's ``query_routes`` block declares, in serving order."""
    raw = _contract().get("query_routes")
    if not isinstance(raw, list) or not raw:
        raise ValueError("contract declares no query_routes")
    return tuple(ModelQueryRoute.model_validate(item) for item in raw)


def contract_mount() -> ModelQueryMount:
    """The contract's ``http_mount`` block: the app factory and the path prefix."""
    return ModelQueryMount.model_validate(_contract().get("http_mount"))


def contract_route(route_id: str) -> ModelQueryRoute:
    """The declared route with this id; ``KeyError`` when the contract has none."""
    for route in contract_routes():
        if route.id == route_id:
            return route
    raise KeyError(f"contract declares no query route {route_id!r}")


__all__ = ["contract_mount", "contract_route", "contract_routes"]
