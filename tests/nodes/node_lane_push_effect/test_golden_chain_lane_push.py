# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_lane_push_effect (OMN-26666): the contract's handler route resolves and answers.

The contract names one handler for the lane-push command topic. The chain reads that
route from ``contract.yaml``, imports the handler and request model it names, and
checks the handler's ``handle`` is the one-request coroutine the runtime calls. Red
until the handler slice lands: the named modules do not exist yet.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any

import yaml

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_lane_push_effect"
)


def _route() -> dict[str, Any]:
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    routes = contract["handler_routing"]["handlers"]
    assert len(routes) == 1
    return dict(routes[0])


def test_golden_chain_the_contract_route_resolves_to_a_one_request_handler() -> None:
    route = _route()
    handler_cls = getattr(
        importlib.import_module(route["handler"]["module"]), route["handler"]["name"]
    )
    request_cls = getattr(
        importlib.import_module(route["event_model"]["module"]),
        route["event_model"]["name"],
    )
    assert inspect.iscoroutinefunction(handler_cls.handle)
    parameters = list(inspect.signature(handler_cls.handle).parameters)
    assert parameters == ["self", "request"]
    assert request_cls.model_config.get("extra") == "forbid"
