# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-state projection's snapshot topic is a publish topic of its contract (OMN-25641).

The runtime's boot provisioner creates a contract's ``event_bus`` topics and nothing else, and the
dev broker has auto-create off, so a snapshot topic named only under ``externally_consumed_topics``
is never created: the publish overlay points the publisher at the dev broker (overlays#729) and the
publish fails on absent cluster metadata. The floor-emissions publish effect declares its snapshot
topic in both blocks; this contract holds the same shape. The topic stays in
``externally_consumed_topics`` because the floor reads it through node_floor_projection_read_effect.
"""

from __future__ import annotations

from pathlib import Path

import yaml

CONTRACT_PATH = (
    Path(__file__).parents[3]
    / "src/omnimarket/nodes/node_work_state_projection_effect/contract.yaml"
)
SNAPSHOT_TOPIC = "onex.snapshot.projection.floor-work-state.v1"


def test_snapshot_topic_is_a_publish_topic() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    publish_topics = contract["event_bus"]["publish_topics"]
    assert SNAPSHOT_TOPIC in publish_topics


def test_snapshot_topic_stays_externally_consumed() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert SNAPSHOT_TOPIC in contract["externally_consumed_topics"]
