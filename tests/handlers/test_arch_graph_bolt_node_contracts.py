# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two architecture graph node contracts name both sources of ``bolt_uri`` (OMN-24687)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_NODES = Path(__file__).resolve().parents[2] / "src" / "omnimarket" / "nodes"


@pytest.mark.unit
@pytest.mark.parametrize(
    "node",
    ["node_architecture_graph_query_effect", "node_architecture_graph_populate_effect"],
)
def test_arch_graph_bolt_description_names_the_host_overlay_row(node: str) -> None:
    contract = yaml.safe_load((_NODES / node / "contract.yaml").read_text())
    description = " ".join(contract["config_fields"]["bolt_uri"]["description"].split())
    assert "ARCH_GRAPH_BOLT_URI" in description
    assert "arch_graph_bolt" in description
    assert "host" in description
