# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab alarm's handlers are bound for the CLI, not routed for boot dispatch.

HandlerBrokerLag and HandlerEffectsHeld take their overlay, readers, stores, dispatcher and clock as
constructor parameters. The boot resolver injects only the event bus, the container and the ownership
query, so a handler_routing entry for either one quarantines it at boot and turns the repository-wide
boot-resolvable guard red on every pull request. The operations are declared under cli_handlers.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path
from typing import Any

import yaml

_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_lab_alarm"
    / "contract.yaml"
)
_OPERATIONS = {"broker_lag", "effects_held"}
_BOOT_INJECTABLE = {"event_bus", "container", "ownership_query"}


def _contract() -> dict[str, Any]:
    contract: dict[str, Any] = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return contract


def test_the_lab_alarm_operations_are_not_boot_routed() -> None:
    routing = _contract().get("handler_routing") or {}
    routed = {entry["operation"] for entry in routing.get("handlers") or []}
    assert not routed & _OPERATIONS


def test_the_lab_alarm_operations_are_bound_under_cli_handlers() -> None:
    bound = {
        entry["operation"]: entry["handler"] for entry in _contract()["cli_handlers"]
    }
    assert set(bound) == _OPERATIONS
    for handler in bound.values():
        cls = getattr(importlib.import_module(handler["module"]), handler["name"])
        required = {
            name
            for name, param in inspect.signature(cls).parameters.items()
            if param.default is inspect.Parameter.empty
            and param.kind
            in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
        }
        assert required - _BOOT_INJECTABLE, (
            f"{handler['name']} is boot-injectable; route it instead of binding it for the CLI"
        )
