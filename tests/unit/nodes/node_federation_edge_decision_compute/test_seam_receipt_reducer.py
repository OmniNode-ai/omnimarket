# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam S3: the forwarder's receipt requests against the shared lease-and-receipt reducer (OMN-24488, plan section 8).

The forwarder side of the seam runs today. The shared reducer (lane lease-build-6803) has no ``unit`` item kind merged, so
the assertions against the reducer's contract are added by the pull request that merges it (plan hold table 7.1, steps S3 and S4).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_federation_edge_decision_compute import (
    overlay as edge_overlay,
)

NODE_DIR = Path(edge_overlay.__file__).resolve().parent
pytestmark = pytest.mark.unit


def test_receipt_topics_and_unit_kind_are_the_reducers() -> None:
    """The forwarder side of seam S3 that exists today: the overlay's three receipt topics and the ``unit`` item kind.

    The reducer's side (lane lease-build-6803) is not merged; the pull request that merges it adds the assertions
    against the reducer's contract here.
    """
    overlay = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
    receipt = overlay["direction"]["receipt"]
    topics = {
        receipt["request_topic"],
        receipt["record_topic"],
        receipt["refusal_topic"],
    }
    assert len(topics) == 3
    assert all(t.startswith("onex.") and t.endswith(".v1") for t in topics)
    assert receipt["item_kind"] == "unit"
