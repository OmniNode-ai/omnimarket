# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The judged acceptance fold node declares a pure handler that resolves."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from typing import Any

import yaml

_ROOT = Path(__file__).resolve().parents[3]
_NODE = "node_projection_delegation_judged_acceptance"
_NODE_DIR = _ROOT / "src" / "omnimarket" / "nodes" / _NODE


def _contract() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((_NODE_DIR / "contract.yaml").read_text())
    return loaded


def _resolve(dotted: str) -> Any:
    module, _, attr = dotted.rpartition(".")
    return getattr(importlib.import_module(module), attr)


def test_contract_names_a_pure_handler_that_resolves() -> None:
    contract = _contract()
    assert contract["descriptor"]["purity"] == "pure"
    handler = contract["handler"]
    cls = getattr(importlib.import_module(handler["module"]), handler["class"])
    assert callable(cls().handle)
    assert (
        _resolve(handler["input_model"]).__name__ == "ModelJudgedAcceptanceFoldRequest"
    )


def test_routed_handler_is_the_declared_handler() -> None:
    contract = _contract()
    (route,) = contract["handler_routing"]["handlers"]
    assert route["operation"] == "judged_acceptance_fold"
    assert route["handler"]["module"] == contract["handler"]["module"]
    assert route["handler"]["name"] == contract["handler"]["class"]


def test_fold_declares_no_topic_so_the_writer_owns_the_bus_wiring() -> None:
    # The subscribe topic and the snapshot belong to the writer change; a routed
    # pure fold would be handed the raw event and cannot report a write.
    assert "event_bus" not in _contract()


def test_node_is_registered_as_an_entry_point() -> None:
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    entry_points = pyproject["project"]["entry-points"]["onex.nodes"]
    assert entry_points[_NODE] == f"omnimarket.nodes.{_NODE}"
    metadata = yaml.safe_load((_NODE_DIR / "metadata.yaml").read_text())
    assert metadata["entry_points"]["onex.nodes"][_NODE] == entry_points[_NODE]
