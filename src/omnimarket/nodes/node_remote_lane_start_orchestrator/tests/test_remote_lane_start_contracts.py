# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""G-NODE for both lane start nodes: the contracts load, name importable handlers and models, and inject spi seams (OMN-22848)."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit]

NODES = Path(__file__).resolve().parents[2]


def _resolve(dotted: str) -> object:
    module, _, name = dotted.replace(":", ".").rpartition(".")
    return getattr(importlib.import_module(module), name)


@pytest.mark.parametrize(
    "node", ["node_remote_lane_start_orchestrator", "node_remote_lane_start_effect"]
)
def test_remote_lane_start_contract_loads_and_names_importable_parts(node: str) -> None:
    contract = yaml.safe_load(
        (NODES / node / "contract.yaml").read_text(encoding="utf-8")
    )
    assert contract["name"] == node
    assert contract["metadata"]["ticket"] == "OMN-22848"
    for route in contract["handler_routing"]["handlers"]:
        handler = _resolve(f"{route['handler']['module']}.{route['handler']['name']}")
        assert callable(getattr(handler, "handle", None))
        _resolve(route["input_model"])
        _resolve(route["output_model"])
    for dep in contract["protocol_dependencies"]:
        assert dep["protocol"].startswith("omnibase_spi.protocols.lane.")
        _resolve(dep["protocol"])
    metadata = yaml.safe_load(
        (NODES / node / "metadata.yaml").read_text(encoding="utf-8")
    )
    assert metadata["name"] == node
    assert (NODES / node / "overlay.example.yaml").is_file()
