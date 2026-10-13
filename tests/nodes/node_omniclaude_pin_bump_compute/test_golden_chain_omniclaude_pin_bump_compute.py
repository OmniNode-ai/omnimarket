# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_omniclaude_pin_bump_compute [OMN-20001].

Verifies the contract and metadata load, the handler is routed from the contract,
and one bump runs end to end through the contract's own handler. The refusals
are pinned by tests/ci/test_omniclaude_release_notice_omn20001.py.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_omniclaude_pin_bump_compute.models import (
    ModelOmniclaudePinBumpRequest,
    ModelOmniclaudePinBumpResult,
)

NODE_NAME = "node_omniclaude_pin_bump_compute"
NODE_DIR = (
    Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes" / NODE_NAME
)
PINNED = (
    "run: git clone --depth=1 --branch v0.27.0 "
    "https://github.com/OmniNode-ai/omniclaude.git ../omniclaude\n"
)


@pytest.fixture
def contract() -> dict[str, Any]:
    return yaml.safe_load((NODE_DIR / "contract.yaml").read_text())


def test_contract_names_the_node_and_declares_no_bus_topics(
    contract: dict[str, Any],
) -> None:
    assert contract["name"] == NODE_NAME
    assert contract["node_type"] == "COMPUTE_GENERIC"
    assert "event_bus" not in contract
    assert contract["descriptor"]["purity"] == "pure"


def test_metadata_registers_the_entry_point() -> None:
    metadata = yaml.safe_load((NODE_DIR / "metadata.yaml").read_text())
    assert metadata["name"] == NODE_NAME
    assert (
        metadata["entry_points"]["onex.nodes"][NODE_NAME]
        == f"omnimarket.nodes.{NODE_NAME}"
    )


def test_contract_handler_bumps_a_pin_end_to_end(contract: dict[str, Any]) -> None:
    handler_block = contract["handler"]
    module = importlib.import_module(handler_block["module"])
    handler = getattr(module, handler_block["class"])()
    result = handler.handle(
        ModelOmniclaudePinBumpRequest(ci_text=PINNED, new_version="0.28.0")
    )
    assert isinstance(result, ModelOmniclaudePinBumpResult)
    assert result.accepted
    assert result.updated_text == PINNED.replace("v0.27.0", "v0.28.0")
