# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC7: topic length bounds, in both forms, before and after prefix removal, and at load."""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_gateway_batch_ingest_effect.overlay import (
    ModelBatchIngestOverlay,
)
from omnimarket.nodes.node_gateway_batch_ingest_effect.topic_rules import (
    TopicRefusedError,
    normalize_topic,
)
from tests.nodes.node_gateway_batch_ingest_effect.fakes import overlay_document

SLUG = "s" * 32
MAX_TOPIC = 100
MAX_WIRE = MAX_TOPIC + len("tenant-") + 32 + 1
CANONICAL_MAX = "t" * MAX_TOPIC
CANONICAL_OVER = "t" * (MAX_TOPIC + 1)


def _normalize(raw: str, allowed: set[str]) -> str:
    return normalize_topic(
        raw,
        slug=SLUG,
        max_topic_chars=MAX_TOPIC,
        max_wire_topic_chars=MAX_WIRE,
        allowed=allowed,
    )


def test_topic_length_maximum_is_accepted_in_both_forms() -> None:
    allowed = {CANONICAL_MAX}
    assert _normalize(CANONICAL_MAX, allowed) == CANONICAL_MAX
    wire = f"tenant-{SLUG}.{CANONICAL_MAX}"
    assert len(wire) == MAX_WIRE
    assert _normalize(wire, allowed) == CANONICAL_MAX


def test_topic_length_one_over_is_refused_in_both_forms() -> None:
    allowed = {CANONICAL_MAX, CANONICAL_OVER}
    for raw in (CANONICAL_OVER, f"tenant-{SLUG}.{CANONICAL_OVER}"):
        with pytest.raises(TopicRefusedError) as caught:
            _normalize(raw, allowed)
        assert caught.value.reason == "topic-too-long"


def test_topic_length_over_long_wire_input_is_refused_before_any_prefix_is_removed() -> (
    None
):
    raw = f"tenant-{SLUG}.{'t' * (MAX_TOPIC + 40)}"
    with pytest.raises(TopicRefusedError) as caught:
        _normalize(raw, {"t" * (MAX_TOPIC + 40)})
    assert caught.value.reason == "topic-too-long"


@pytest.mark.parametrize(
    "limits",
    [
        {"max_wire_topic_chars": 250},
        {"max_wire_topic_chars": 140, "max_topic_chars": 108},
        {"max_topic_chars": 40},
    ],
)
def test_topic_length_load_refuses_the_inequalities(limits: dict[str, int]) -> None:
    document = copy.deepcopy(overlay_document())
    document["limits"].update(limits)
    with pytest.raises(ValidationError):
        ModelBatchIngestOverlay.model_validate(document)


def test_topic_length_load_accepts_the_boundary() -> None:
    document = copy.deepcopy(overlay_document())
    document["limits"].update({"max_topic_chars": 109, "max_wire_topic_chars": 149})
    document["topics"] = [t for t in document["topics"] if len(t["topic"]) <= 109]
    ModelBatchIngestOverlay.model_validate(document)
