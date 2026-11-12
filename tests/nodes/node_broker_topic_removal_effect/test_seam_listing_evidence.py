# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seams of the broker topic removal (OMN-23697), each failing when either side changes shape.

1. The never-produced listing (node_topic_never_produced_compute, OMN-23614, lanes topics-2835 and redfix-b-3670):
   the removal builds that node's evidence model from the broker's offsets and its rule from its own overlay, so the
   fields it fills and the keys it passes must be exactly the listing's.
2. The broker load reading (node_broker_load_reading_effect, rebuild piece 3): both read the same public metrics, so one
   metrics text must give the same topic and partition counts in both, and the empty partitions of the topics the removal
   calls never produced can never exceed the load reading's empty partitions.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.models.topic_never_produced import (
    ModelNeverProducedRule,
    ModelTopicEvidence,
)
from omnimarket.nodes.node_broker_load_reading_effect.adapters.metrics_source import (
    parse_public_metrics,
)
from omnimarket.nodes.node_broker_topic_removal_effect.adapters.topic_inventory import (
    parse_topic_observations,
)
from omnimarket.nodes.node_broker_topic_removal_effect.overlay import (
    ModelTopicRemovalOverlay,
)

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_broker_topic_removal_effect"
)
FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures/broker_load/public_metrics_dev_2026-11-06.prom"
)
#: What the removal fills on the listing's evidence model; a field added or dropped there changes this set.
FILLED_EVIDENCE_FIELDS = {
    "name",
    "partitions",
    "high_watermark_total",
    "records_in_retention",
    "consumer_groups",
    "declared_producers",
}


def test_seam_listing_evidence_fields_are_exactly_the_ones_the_removal_fills() -> None:
    assert set(ModelTopicEvidence.model_fields) == FILLED_EVIDENCE_FIELDS


def test_seam_listing_rule_keys_are_exactly_the_overlay_listing_rule_keys() -> None:
    example = yaml.safe_load((NODE / "overlay.example.yaml").read_text("utf-8"))
    assert set(example["listing_rule"]) == set(ModelNeverProducedRule.model_fields)
    overlay = ModelTopicRemovalOverlay.model_validate(example)
    assert overlay.listing_rule == ModelNeverProducedRule.model_validate(
        example["listing_rule"]
    )


def test_seam_load_reading_and_removal_agree_on_the_same_metrics_text() -> None:
    text = FIXTURE.read_text("utf-8")
    load = parse_public_metrics(text)
    observations = parse_topic_observations(text)
    never_produced_partitions = sum(
        obs.partitions for obs in observations if obs.high_watermark_total == 0
    )
    # the removal reads user topics only, the load reading counts every cluster partition
    assert sum(obs.partitions for obs in observations) <= load.partitions
    assert len(observations) <= load.topics
    assert never_produced_partitions <= load.empty_partitions
    assert never_produced_partitions > 0


def test_seam_control_a_changed_evidence_shape_would_be_caught() -> None:
    assert FILLED_EVIDENCE_FIELDS | {"retention_ms"} != set(
        ModelTopicEvidence.model_fields
    )
