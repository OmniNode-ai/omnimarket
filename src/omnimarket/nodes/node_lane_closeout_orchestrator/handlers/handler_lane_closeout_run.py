# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Inventory a lane, then close out its trees through contract-routed handlers."""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable
from importlib.resources import files
from pathlib import Path
from typing import Protocol, cast

import yaml
from pydantic import BaseModel

from omnimarket.models.lane_closeout.model_lane_closeout_request import (
    ModelLaneCloseoutRequest,
)
from omnimarket.models.lane_closeout.model_lane_closeout_result import (
    ModelLaneCloseoutResult,
)
from omnimarket.models.lane_closeout.model_lane_closeout_run_request import (
    ModelLaneCloseoutRunRequest,
)
from omnimarket.models.lane_closeout.model_lane_closeout_run_result import (
    ModelLaneCloseoutRunResult,
)
from omnimarket.models.lane_closeout.model_lane_tree_inventory_request import (
    ModelLaneTreeInventoryRequest,
)
from omnimarket.models.lane_closeout.model_lane_tree_inventory_result import (
    ModelLaneTreeInventoryResult,
)

INVENTORY_EDGE: tuple[str, str] = (
    "node_lane_tree_inventory_effect",
    "inventory_lane_trees",
)
CLOSEOUT_EDGE: tuple[str, str] = ("node_lane_closeout_effect", "close_lane_tree")
EDGES: tuple[tuple[str, str], ...] = (INVENTORY_EDGE, CLOSEOUT_EDGE)


class _ContractHandler(Protocol):
    """The runtime call boundary, with result models validated at each edge."""

    def handle(self, request: BaseModel) -> object: ...


class _HandlerReference(BaseModel):
    name: str
    module: str


class _HandlerRoute(BaseModel):
    operation: str
    handler: _HandlerReference


class _HandlerRouting(BaseModel):
    handlers: tuple[_HandlerRoute, ...]


class _Contract(BaseModel):
    handler_routing: _HandlerRouting


def _contract_handler(node: str, operation: str) -> _ContractHandler:
    """Instantiate the handler a sibling contract names for an operation."""
    contract = _Contract.model_validate(
        yaml.safe_load(
            files(f"omnimarket.nodes.{node}").joinpath("contract.yaml").read_text()
        )
    )
    for entry in contract.handler_routing.handlers:
        if entry.operation == operation:
            module = importlib.import_module(entry.handler.module)
            factory = cast(
                "Callable[[], _ContractHandler]", getattr(module, entry.handler.name)
            )
            return factory()
    raise LookupError(f"{node} routes no operation {operation!r}")


class HandlerLaneCloseoutRun:
    """Capture the full inventory before closing out any tree."""

    def handle(
        self, request: ModelLaneCloseoutRunRequest
    ) -> ModelLaneCloseoutRunResult:
        node, operation = INVENTORY_EDGE
        inventory = _contract_handler(node, operation).handle(
            ModelLaneTreeInventoryRequest(
                lane_dir=request.lane_dir, correlation_id=request.correlation_id
            )
        )
        if not isinstance(inventory, ModelLaneTreeInventoryResult):
            raise TypeError(f"{node} returned {type(inventory).__name__}")

        closeouts: list[ModelLaneCloseoutResult] = []
        parts: list[str] = []
        for line in inventory.trees:
            relative = Path(line.path).relative_to(request.lane_dir).as_posix()
            tag = re.sub(
                r"[^A-Za-z0-9._-]+", "-", f"{request.lane}-{request.run_id}-{relative}"
            ).strip("-.")
            node, operation = CLOSEOUT_EDGE
            closeout = _contract_handler(node, operation).handle(
                ModelLaneCloseoutRequest(
                    correlation_id=request.correlation_id,
                    lane=request.lane,
                    tree=line.path,
                    tag=tag,
                    remove=request.remove,
                )
            )
            if not isinstance(closeout, ModelLaneCloseoutResult):
                raise TypeError(f"{node} returned {type(closeout).__name__}")
            closeouts.append(closeout)
            part = closeout.cell
            if closeout.wip_push:
                part += f" (wip-pushed={closeout.wip_push})"
            parts.append(part)

        summary = (
            "lane closeout: " + "; ".join(parts)
            if parts
            else "lane closeout: the lane made no worktrees"
        )
        return ModelLaneCloseoutRunResult(
            correlation_id=request.correlation_id,
            lane=request.lane,
            inventory=inventory,
            closeouts=tuple(closeouts),
            summary=summary,
        )
