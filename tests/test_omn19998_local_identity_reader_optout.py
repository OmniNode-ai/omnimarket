# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local-identity bus exposure declares a reasoned, self-expiring reader opt-out."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_local_identity/contract.yaml"
)


def test_bus_backed_exposure_declares_a_reasoned_reader_opt_out() -> None:
    """exposure-reader-coverage refuses a bus_backed exposure with no reader."""
    projection_api = yaml.safe_load(CONTRACT.read_text())["projection_api"]
    assert projection_api["bus_backed"] is True
    assert projection_api["consumers"] == "none"
    reason = projection_api["consumers_reason"]
    assert isinstance(reason, str)
    assert reason.strip()
    assert "onex.snapshot.projection.local-identity.v1" in reason


def test_opt_out_leaves_the_exposure_bus_backed_and_keyed_unchanged() -> None:
    """The opt-out is additive: the shape the node published before is intact."""
    projection_api = yaml.safe_load(CONTRACT.read_text())["projection_api"]
    assert projection_api["bus_backed"] is True
    assert projection_api["key_columns"] == ["tenant_id"]
    assert projection_api["tenant_column"] == "tenant_id"
    assert projection_api["consumers"] == "none"


def test_opted_out_exposure_is_still_discovered_for_serving() -> None:
    """With the opt-out declared the topic is still in the discovered topic map."""
    from omnimarket.projection.discovery import build_projection_topic_map

    topic = "onex.snapshot.projection.local-identity.v1"
    assert topic in build_projection_topic_map()
    projection_api = yaml.safe_load(CONTRACT.read_text())["projection_api"]
    assert projection_api["consumers"] == "none"
