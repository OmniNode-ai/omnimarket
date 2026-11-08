# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared loaders and mutations for the routing overlay check tests (OMN-22976)."""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from omnimarket.nodes.node_delegation_routing_overlay_check_compute.handlers.handler_delegation_routing_overlay_check import (
    HandlerDelegationRoutingOverlayCheck,
)
from omnimarket.nodes.node_delegation_routing_overlay_check_compute.models.model_routing_overlay_check import (
    EnumOverlayFindingClass,
    ModelRoutingOverlayCheckRequest,
    ModelRoutingOverlayCheckResult,
)
from omnimarket.nodes.node_delegation_routing_overlay_check_compute.models.model_task_class_bar import (
    ModelTaskClassBar,
)

CONFIGS = Path(__file__).resolve().parents[3] / "configs"
CONTRACTS_PATH = CONFIGS / "task_class_contracts.v1.yaml"
OVERLAY_PATH = CONFIGS / "task_class_bars.v1.yaml"
CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def load(path: Path) -> dict[str, object]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def contracts() -> dict[str, object]:
    return copy.deepcopy(load(CONTRACTS_PATH))


def overlay() -> dict[str, object]:
    return copy.deepcopy(load(OVERLAY_PATH))


def class_names() -> list[str]:
    classes = contracts()["task_classes"]
    assert isinstance(classes, dict)
    return list(classes)


def bars(doc: dict[str, object]) -> dict[str, dict[str, object]]:
    slot = doc["task_class_bars"]
    assert isinstance(slot, dict)
    return slot


def bar_body(doc: dict[str, object], name: str) -> dict[str, object]:
    body = bars(doc)[name]["bar"]
    assert isinstance(body, dict)
    return body


def contract_class(doc: dict[str, object], name: str) -> dict[str, object]:
    classes = doc["task_classes"]
    assert isinstance(classes, dict)
    entry = classes[name]
    assert isinstance(entry, dict)
    return entry


def check(
    contract_doc: dict[str, object] | None = None,
    overlay_doc: dict[str, object] | None = None,
) -> ModelRoutingOverlayCheckResult:
    return HandlerDelegationRoutingOverlayCheck().handle(
        ModelRoutingOverlayCheckRequest(
            task_class_contracts=contract_doc
            if contract_doc is not None
            else contracts(),
            bars_overlay=overlay_doc if overlay_doc is not None else overlay(),
        )
    )


def findings(
    result: ModelRoutingOverlayCheckResult,
) -> set[tuple[str, EnumOverlayFindingClass]]:
    return {(f.task_class, f.finding_class) for f in result.findings}


def other_required_bar(value: float) -> float:
    """A required bar that differs from ``value`` and stays a valid score."""
    return 0.5 if value != 0.5 else 0.6


def refreeze(doc: dict[str, object], name: str) -> None:
    """Recompute the hash of one class's bar from the bar as it now stands."""
    bar = ModelTaskClassBar.model_validate(bar_body(doc, name))
    bars(doc)[name]["bar_hash"] = bar.frozen_hash(name)
