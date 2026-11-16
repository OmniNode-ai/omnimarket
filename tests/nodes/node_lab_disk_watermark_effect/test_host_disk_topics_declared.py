# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23865: each host disk event is declared by the node that produces it, under its literal topic name."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

NODES = Path(__file__).resolve().parents[3] / "src" / "omnimarket" / "nodes"


def _contract(node: str) -> dict[str, Any]:
    loaded = yaml.safe_load((NODES / node / "contract.yaml").read_text())
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.unit
def test_the_watermark_effect_publishes_the_reading_and_its_typed_failure() -> None:
    contract = _contract("node_lab_disk_watermark_effect")
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.host-disk-read.v1",
        "onex.evt.omnimarket.host-disk-read-failed.v1",
    ]
    assert contract["terminal_event"] == "onex.evt.omnimarket.host-disk-read.v1"


@pytest.mark.unit
def test_the_watermark_effect_is_started_by_its_timer() -> None:
    contract = _contract("node_lab_disk_watermark_effect")
    assert "onex-disk-watermark" in contract["runtime_dispatch"]["external_trigger"]


@pytest.mark.unit
def test_the_projection_consumes_the_reading_and_publishes_its_applied_event() -> None:
    contract = _contract("node_projection_host_disk")
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.evt.omnimarket.host-disk-read.v1"
    ]
    assert contract["event_bus"]["publish_topics"] == [
        "onex.evt.omnimarket.projection-host-disk-applied.v1"
    ]
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.projection-host-disk-applied.v1"
    )
