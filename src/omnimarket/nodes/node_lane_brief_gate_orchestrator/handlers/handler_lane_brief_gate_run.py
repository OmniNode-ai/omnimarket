# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the brief gate: its first edge parses the brief into ``ModelLaneBrief``.

The orchestrator imports no sibling node's handler or model package. An edge is found the way the
runtime finds it: the sibling's ``contract.yaml`` names the handler for the operation, and that
handler is loaded and called with the shared request model. Later edges join ``EDGES`` in order.
"""

from __future__ import annotations

import importlib
from importlib.resources import files
from typing import Any

import yaml

from omnimarket.models.lane_brief.model_lane_brief_parse_request import (
    ModelLaneBriefParseRequest,
)
from omnimarket.models.lane_brief.model_lane_brief_parse_result import (
    ModelLaneBriefParseResult,
)

PARSE_EDGE: tuple[str, str] = ("node_lane_brief_parse_compute", "parse_lane_brief")
EDGES: tuple[tuple[str, str], ...] = (PARSE_EDGE,)


def _contract_handler(node: str, operation: str) -> Any:
    """Instantiate the handler a node's contract routes ``operation`` to."""
    contract = yaml.safe_load(
        files(f"omnimarket.nodes.{node}").joinpath("contract.yaml").read_text()
    )
    for entry in contract["handler_routing"]["handlers"]:
        if entry["operation"] == operation:
            module = importlib.import_module(entry["handler"]["module"])
            return getattr(module, entry["handler"]["name"])()
    raise LookupError(f"{node} routes no operation {operation!r}")


class HandlerLaneBriefGateRun:
    """Flow only: the gate decides nothing itself."""

    def handle(self, request: ModelLaneBriefParseRequest) -> ModelLaneBriefParseResult:
        node, operation = PARSE_EDGE
        result = _contract_handler(node, operation).handle(request)
        if not isinstance(result, ModelLaneBriefParseResult):
            raise TypeError(f"{node} returned {type(result).__name__}")
        return result
