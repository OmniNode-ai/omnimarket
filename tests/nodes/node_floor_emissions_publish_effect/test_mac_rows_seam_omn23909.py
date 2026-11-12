# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The seam between the chain publishers and the overlays repository's broker rows (OMN-23909).

The overlays repository's scope ``broker_acls_mac_floor_emissions`` grants the Mac login exactly the four floor emission
topics below, so every topic a publisher writes to or reads must be one of them: when a publisher renames a topic,
the seam fails and the rows no longer cover the write.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
import yaml

from omnimarket.handlers.emission_mirror import InMemoryEmissionMirror
from omnimarket.handlers.emission_sink import ChainEmitter, JsonlEmissionSink
from omnimarket.models.floor_17a.model_floor_17a import ChainOrigin
from omnimarket.models.floor_projection import (
    ModelEmissionMirrorOverlay,
    ModelFloorEmissionsOverlay,
)

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_floor_emissions_publish_effect"
)
MIRROR = ModelEmissionMirrorOverlay.model_validate(
    yaml.safe_load((NODE_DIR / "overlay.mirror.example.yaml").read_text())
)
PROJECTION = ModelFloorEmissionsOverlay.model_validate(
    yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
)
FLOOR_EMISSION_ROW_TOPICS = [
    "onex.evt.omnimarket.floor-node-started.v1",
    "onex.evt.omnimarket.floor-node-succeeded.v1",
    "onex.evt.omnimarket.floor-node-failed.v1",
    "onex.snapshot.projection.floor-emissions.v1",
]

NOW = datetime(2026, 11, 11, 20, 0, tzinfo=UTC)


def write_chain(
    sink: JsonlEmissionSink, origin: str, cid: str, *, fail: bool = False
) -> None:
    emitter = ChainEmitter(
        sink,
        node="node_x:x",
        piece=3,
        clock=lambda: NOW,
        correlation_id=cid,
        origin=cast(ChainOrigin, origin),
    )
    emitter.start()
    if fail:
        emitter.failure("x_failed", "planted")
    else:
        emitter.success({"n": 1})


def the_topics_the_publishers_write_and_read() -> set[str]:
    return {
        MIRROR.topic_start,
        MIRROR.topic_success,
        MIRROR.topic_failure,
        PROJECTION.snapshot_topic,
    }


def test_seam_the_topics_the_publishers_write_and_read_are_the_topics_the_rows_grant() -> (
    None
):
    assert the_topics_the_publishers_write_and_read() == set(FLOOR_EMISSION_ROW_TOPICS)


def test_golden_chain_on_the_in_memory_bus_every_phase_topic_gets_the_event(
    tmp_path: Path,
) -> None:
    mirror = InMemoryEmissionMirror(MIRROR)
    sink = JsonlEmissionSink(tmp_path / "e.jsonl", mirror)
    write_chain(sink, "real", "a")
    write_chain(sink, "real", "b")
    write_chain(sink, "probe", "c", fail=True)
    assert [topic for topic, _event in mirror.published] == [
        MIRROR.topic_start,
        MIRROR.topic_success,
        MIRROR.topic_start,
        MIRROR.topic_success,
        MIRROR.topic_start,
        MIRROR.topic_failure,
    ]
    assert {topic for topic, _event in mirror.published} == {
        MIRROR.topic_start,
        MIRROR.topic_success,
        MIRROR.topic_failure,
    }


def test_control_a_renamed_topic_breaks_the_seam() -> None:
    renamed = ModelEmissionMirrorOverlay.model_validate(
        {
            **MIRROR.model_dump(),
            "topic_success": "onex.evt.omnimarket.floor-node-succeeded.v2",
        }
    )
    assert {
        renamed.topic_start,
        renamed.topic_success,
        renamed.topic_failure,
        PROJECTION.snapshot_topic,
    } != set(FLOOR_EMISSION_ROW_TOPICS)
