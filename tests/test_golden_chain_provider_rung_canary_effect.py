# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16987 golden chain: the canary node's contract, handler and entry point agree.

The scheduled workflow and the ``onex skill provider_rung_canary`` mapping live
in another repository and shim over this node; this test pins the half they
depend on, so renaming the node, its operation or its handler fails here.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
NODE = "node_provider_rung_canary_effect"
NODE_DIR = ROOT / "src" / "omnimarket" / "nodes" / NODE


def _contract() -> dict[str, Any]:
    return yaml.safe_load((NODE_DIR / "contract.yaml").read_text(encoding="utf-8"))


@pytest.mark.unit
def test_contract_resolves_handler_and_models() -> None:
    contract = _contract()
    handler = contract["handler"]
    module = importlib.import_module(handler["module"])
    cls = getattr(module, handler["class"])
    assert callable(cls().handle)
    [route] = contract["handler_routing"]["handlers"]
    assert route["operation"] == "probe_provider_rungs"
    for key in ("input_model", "output_model"):
        mod, _, name = route[key].rpartition(".")
        assert hasattr(importlib.import_module(mod), name)


@pytest.mark.unit
def test_contract_declares_one_command_and_one_terminal_event() -> None:
    contract = _contract()
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [contract["runtime_dispatch"]["command_topic"]]
    assert bus["publish_topics"] == [contract["terminal_event"]]
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.provider-rung-canary-completed.v1"
    )
    assert contract["terminal_event"] in contract["externally_consumed_topics"]


@pytest.mark.unit
def test_node_is_an_onex_nodes_entry_point() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    entry = pyproject["project"]["entry-points"]["onex.nodes"][NODE]
    assert entry == f"omnimarket.nodes.{NODE}"
    meta = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text(encoding="utf-8"))
    assert meta["entry_points"]["onex.nodes"][NODE] == entry
    assert meta["capabilities"]["requires_secrets"] is True
