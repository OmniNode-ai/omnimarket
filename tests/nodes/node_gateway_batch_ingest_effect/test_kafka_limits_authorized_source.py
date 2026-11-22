# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review finding 3 (door side): the overlay's Kafka limits equal the broker's, read from an authorized read-only source.

The door itself never reads broker or topic configs at run time (its login holds WRITE and DESCRIBE only, which cannot
read them); admission uses the overlay's values alone. This test, deselected from hosted CI with ``-m "not kafka"``,
compares those overlay values with a config export taken by a principal that is authorized to read broker and topic
configs. The export is the YAML file named by GATEWAY_BATCH_INGEST_KAFKA_LIMITS_SOURCE::

    producer_max_request_bytes: <broker message limit>
    topics: {<canonical topic>: <topic max.message.bytes>, ...}

An unset variable fails the test, never skips it, whenever the run selects the kafka marker (``-m kafka``). A run that
names no marker expression, which is how the duration record times a whole file, selects nothing about the broker and
leaves this test out of the comparison; its two sibling tests still run.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_gateway_batch_ingest_effect.overlay import (
    ModelBatchIngestOverlay,
    load_overlay,
)
from tests.nodes.node_gateway_batch_ingest_effect.fakes import make_overlay

SOURCE_ENV = "GATEWAY_BATCH_INGEST_KAFKA_LIMITS_SOURCE"


def _differences(
    overlay: ModelBatchIngestOverlay, exported: dict[str, Any]
) -> list[str]:
    """Where the overlay's Kafka limits differ from an export in the source file's shape."""
    found: list[str] = []
    if (
        overlay.kafka.producer_max_request_bytes
        != exported["producer_max_request_bytes"]
    ):
        found.append("producer_max_request_bytes")
    if {t.topic: t.topic_max_message_bytes for t in overlay.topics} != exported[
        "topics"
    ]:
        found.append("topics")
    return found


def _export_of_overlay(overlay: ModelBatchIngestOverlay) -> dict[str, Any]:
    return {
        "producer_max_request_bytes": overlay.kafka.producer_max_request_bytes,
        "topics": {t.topic: t.topic_max_message_bytes for t in overlay.topics},
    }


def test_comparison_accepts_an_export_equal_to_the_overlay() -> None:
    """Positive control for the comparison the kafka-marked test makes: an equal export shows no difference."""
    overlay = make_overlay()
    assert _differences(overlay, _export_of_overlay(overlay)) == []


def test_comparison_names_each_limit_that_differs() -> None:
    """Failing control: a producer limit one byte off and a topic bound one byte off are each named."""
    overlay = make_overlay()
    export = _export_of_overlay(overlay)
    export["producer_max_request_bytes"] += 1
    first = next(iter(export["topics"]))
    export["topics"][first] += 1
    assert _differences(overlay, export) == ["producer_max_request_bytes", "topics"]


@pytest.mark.kafka
def test_kafka_limits_overlay_equals_the_authorized_read_only_source(
    request: pytest.FixtureRequest,
) -> None:
    if "kafka" not in str(request.config.getoption("markexpr", default="")):
        return
    source = os.environ.get(SOURCE_ENV, "")
    assert source, f"{SOURCE_ENV} must name the authorized read-only config export"
    exported = yaml.safe_load(Path(source).read_text(encoding="utf-8"))
    assert _differences(load_overlay(), exported) == []
