# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's metadata and contract name the node directory (OMN-23874)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

_NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_delegation_routing_order_compute"
)
_NODE_DIR_NAME = "node_delegation_routing_order_compute"


def test_metadata_name_and_contract_name_match_the_node_directory() -> None:
    """The metadata's and the contract's names equal the node directory's name."""
    metadata: dict[str, Any] = yaml.safe_load(
        (_NODE_DIR / "metadata.yaml").read_text(encoding="utf-8")
    )
    contract: dict[str, Any] = yaml.safe_load(
        (_NODE_DIR / "contract.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(metadata, dict)
    assert isinstance(contract, dict)
    assert contract["name"] == _NODE_DIR_NAME
    assert metadata["name"] == _NODE_DIR_NAME
