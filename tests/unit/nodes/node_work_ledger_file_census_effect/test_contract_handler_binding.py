# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The census contract binds every handler the node owns without routing a handler the runtime cannot build.

Three repository gates read this one contract and pull in different directions unless the handlers are bound the
way the in-process handlers of other nodes are: the canonical-file-shape gate refuses a handler file no contract
binds, the compliance sweep refuses a ``handler_routing`` table that leaves out the canonical handler, and the
boot-resolvable guard refuses a routed handler with a constructor parameter the runtime cannot inject.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import yaml

from omnimarket.nodes.node_work_ledger_file_census_effect.handlers.handler_daily_change import (
    HandlerDailyChange,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.handlers.handler_daily_publication import (
    HandlerDailyPublication,
)
from omnimarket.nodes.node_work_ledger_file_census_effect.handlers.handler_file_census import (
    HandlerFileCensus,
)

_NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_work_ledger_file_census_effect"
)
_RUNTIME_INJECTABLE = frozenset({"event_bus", "container", "ownership_query"})


def _contract() -> dict[str, object]:
    loaded = yaml.safe_load((_NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _bound_modules(node: object) -> set[str]:
    """Every ``handler.module`` string anywhere in the document (what the file-shape gate reads)."""
    found: set[str] = set()
    if isinstance(node, dict):
        handler = node.get("handler")
        if isinstance(handler, dict) and isinstance(handler.get("module"), str):
            found.add(handler["module"])
        for value in node.values():
            found |= _bound_modules(value)
    elif isinstance(node, list):
        for item in node:
            found |= _bound_modules(item)
    return found


def _handler_modules_on_disk() -> set[str]:
    return {
        f"omnimarket.nodes.node_work_ledger_file_census_effect.handlers.{path.stem}"
        for path in (_NODE_DIR / "handlers").glob("handler_*.py")
    }


def test_every_handler_file_is_bound_by_the_contract() -> None:
    assert _handler_modules_on_disk() <= _bound_modules(_contract())


def test_no_routed_handler_needs_a_parameter_the_runtime_cannot_inject() -> None:
    routing = _contract().get("handler_routing") or {}
    assert isinstance(routing, dict)
    routed = {
        entry["handler"]["name"]
        for entry in routing.get("handlers") or []
        if isinstance(entry, dict)
    }
    constructors = {
        "HandlerFileCensus": HandlerFileCensus,
        "HandlerDailyChange": HandlerDailyChange,
        "HandlerDailyPublication": HandlerDailyPublication,
    }
    for name in routed:
        required = [
            param
            for param_name, param in inspect.signature(
                constructors[name]
            ).parameters.items()
            if param_name not in _RUNTIME_INJECTABLE
            and param.default is inspect.Parameter.empty
            and param.kind
            in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        ]
        assert not required, f"{name} is routed but needs {required} at construction"
