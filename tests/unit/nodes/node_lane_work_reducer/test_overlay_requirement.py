# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three lane work nodes carry a bootstrap overlay.example.yaml and no document in the overlay source (OMN-26721).

The overlay check refuses an added node whose directory holds overlay.example.yaml while the overlay source has no document for
it. The reducer's example is the bootstrap of values the orchestrator's overlay carries, so the node declares its overlay
optional and the check does not ask the overlay source for a document nothing reads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_overlay_main_load_check_effect.handlers.handler_overlay_required_check import (
    overlay_requirement,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[4] / "src/omnimarket/nodes/node_lane_work_reducer"
)


NODES = (
    "node_lane_work_reducer",
    "node_lane_work_orchestrator",
    "node_lane_work_event_classify_compute",
)


@pytest.mark.parametrize("node", NODES)
def test_node_declares_its_overlay_optional(node: str) -> None:
    requirement = overlay_requirement(NODE_DIR.parent / node)
    assert requirement is not None
    assert requirement.required is False


def test_a_node_without_the_declaration_is_still_required(tmp_path: Path) -> None:
    """Known bad: a directory holding only the example file reads as requiring an overlay."""
    node = tmp_path / "node_x"
    node.mkdir()
    (node / "overlay.example.yaml").write_text("a: 1\n", encoding="utf-8")
    requirement = overlay_requirement(node)
    assert requirement is not None
    assert requirement.required is True
    assert requirement.source == "example_file"
