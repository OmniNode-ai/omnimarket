# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane work reducer's overlay is bootstrap only, so the overlay load check does not require it.

The reducer reads no file: its configuration arrives as input, and overlay.example.yaml holds the
bootstrap values. No deployed overlay document exists for it, so a contract that left the overlay
required would fail Overlay Main Load on every hosted runner.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_overlay_main_load_check_effect.handlers.handler_overlay_required_check import (
    overlay_requirement,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_lane_work_reducer"
)


def test_the_reducer_overlay_is_declared_optional() -> None:
    requirement = overlay_requirement(NODE_DIR)
    assert requirement is not None
    assert requirement.required is False
    assert requirement.overlay_id == "node_lane_work_reducer"


def test_the_bootstrap_example_is_still_shipped() -> None:
    assert (NODE_DIR / "overlay.example.yaml").is_file()
