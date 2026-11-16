# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Structural integration gaps remain visible and carry an actionable recommendation."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.floor_projection import ModelFloorProjectionPublishOverlay
from omnimarket.models.integration_reading import (
    ModelIntegrationProjection,
    ModelIntegrationReadingBatch,
)
from omnimarket.nodes.node_floor_owner_map_publish_effect.handlers import (
    InMemoryEnvelopePublisher,
)
from omnimarket.nodes.node_integration_projection_compute.handlers import (
    HandlerIntegrationProjection,
)
from omnimarket.nodes.node_integration_reading_publish_effect import (
    runtime_integration_reading_publish as runtime,
)

pytestmark = pytest.mark.unit


def batch(**overrides: Any) -> dict[str, Any]:
    reading = {
        "repository": "omnimarket",
        "number": 5404,
        "title": "validator workflow",
        "merged_at": "2026-11-08T01:00:00Z",
        "target": "validator workflow host",
        "state": "no_caller",
        "gap_class": "needs_reader",
        "gap": "No production caller schedules this workflow",
        "step": "Add the workflow caller before scheduling a proving lane",
        "recommendation": "WIRE",
        "recommendation_target": "pull-request change reader; effects profile",
        "read_at": "2026-11-16T22:43:48Z",
        "origin": "integration-pass",
    }
    reading.update(overrides)
    return {
        "as_of": "2026-11-16T22:43:48Z",
        "origin": "integration-pass",
        "readings": [reading],
    }


@pytest.mark.parametrize("recommendation", ["WIRE", "RETIRE", "MOVE"])
def test_no_caller_reader_and_projection_preserve_recommendation(
    recommendation: str,
) -> None:
    request = ModelIntegrationReadingBatch.model_validate_json(
        json.dumps(batch(recommendation=recommendation))
    )
    projection = HandlerIntegrationProjection().handle(request)
    restored = ModelIntegrationProjection.model_validate_json(
        projection.model_dump_json()
    )
    row = restored.rows["omnimarket#5404"]
    assert row.state.value == "no_caller"
    assert row.recommendation == recommendation
    assert row.recommendation_target == "pull-request change reader; effects profile"
    # A structural gap is still outstanding work, never counted as running or settled.
    assert restored.counts.unintegrated == 1
    assert restored.counts.seen_running == restored.counts.settled_elsewhere == 0
    assert restored.groups[0].keys == ("omnimarket#5404",)


@pytest.mark.parametrize(
    "overrides",
    [
        {"recommendation": None},
        {"recommendation": "RUN"},
        {"recommendation_target": ""},
        {"recommendation_target": "   "},
        {"gap": ""},
        {"step": ""},
        {"gap_class": "none"},
    ],
)
def test_no_caller_refuses_an_unactionable_reading(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ModelIntegrationReadingBatch.model_validate(batch(**overrides))


def test_ordinary_readings_need_no_recommendation() -> None:
    raw = batch(state="not_running")
    del raw["readings"][0]["recommendation"]
    del raw["readings"][0]["recommendation_target"]
    request = ModelIntegrationReadingBatch.model_validate(raw)
    assert HandlerIntegrationProjection().handle(request).counts.unintegrated == 1


@pytest.mark.asyncio
async def test_existing_pass_publisher_carries_no_caller_without_a_node_change() -> (
    None
):
    request = ModelIntegrationReadingBatch.model_validate_json(json.dumps(batch()))
    publisher = InMemoryEnvelopePublisher()
    result = await runtime.run(
        request,
        ModelFloorProjectionPublishOverlay(bus="inmemory", projection_id="integration"),
        publisher,
        InMemoryEmissionSink(),
        producer="integration-pass",
        clock=lambda: datetime(2026, 11, 16, 22, 43, 48, tzinfo=UTC),
    )
    assert result.published
    assert result.entries == 1
    projection = ModelIntegrationProjection.model_validate(
        publisher.published[0].payload
    )
    assert projection.rows["omnimarket#5404"].state.value == "no_caller"
    assert projection.rows["omnimarket#5404"].recommendation == "WIRE"
    assert projection.counts.unintegrated == 1


def test_reviewed_inventory_covers_the_source_prs_and_names_each_target() -> None:
    inventory = json.loads(
        Path(__file__).with_name("no_caller_inventory.json").read_text()
    )
    assert {row["pr"] for row in inventory["source_prs"]} == {
        5404,
        5628,
        5685,
        5699,
        5715,
        5716,
        5879,
        5289,
        5502,
        5503,
        5546,
        5641,
        5657,
    }
    source_nodes = {node for pr in inventory["source_prs"] for node in pr["nodes"]}
    classified = {row["node"] for row in inventory["rows"]}
    excluded = {row["node"] for row in inventory["reachable_exclusions"]}
    assert not classified & excluded
    assert classified | excluded == source_nodes
    counts = Counter(row["recommendation"] for row in inventory["rows"])
    assert {key: counts[key] for key in inventory["counts"]} == inventory["counts"]
    for row in inventory["rows"]:
        assert row["ticket_purpose"].strip()
        assert row["caller_or_flow"].strip()
        assert row["recommendation"] in {"WIRE", "RETIRE", "MOVE"}
        assert row["recommendation_target"].strip()
        assert row["graph_answered_from"] == inventory["source_commit"]
    for row in inventory["reachable_exclusions"]:
        assert row["caller"].strip()
