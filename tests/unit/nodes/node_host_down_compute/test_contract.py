# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract validation for node_host_down_compute (OMN-19998).

The contract is the node's interface, so these assertions are about promises
the rest of the platform is allowed to rely on: the node names itself, is a
generic compute node, the handler the contract names actually imports and
answers ``handle``, and the node neither subscribes nor publishes — a pure
compute node is invoked, not delivered to.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

import omnimarket.nodes.node_host_down_compute as node_package

pytestmark = [pytest.mark.unit]

CONTRACT_PATH = Path(node_package.__file__).resolve().parent / "contract.yaml"


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), "contract.yaml must load as a mapping"
    return raw


def test_the_contract_names_the_node(contract: dict[str, Any]) -> None:
    assert contract["name"] == "node_host_down_compute"


def test_the_node_is_a_generic_compute_node(contract: dict[str, Any]) -> None:
    assert contract["node_type"] == "COMPUTE_GENERIC"


def test_the_declared_handler_imports_and_answers_handle(
    contract: dict[str, Any],
) -> None:
    handler_block = contract["handler"]
    module = importlib.import_module(handler_block["module"])
    handler_class = getattr(module, handler_block["class"])
    assert callable(getattr(handler_class, "handle", None))


def test_the_node_subscribes_to_and_publishes_nothing(
    contract: dict[str, Any],
) -> None:
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == []
    assert bus["publish_topics"] == []
