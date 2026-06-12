# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression lock for OMN-13067: gate.activity + gate.metrics projection topics
must be discoverable from node_omnigate_projection/contract.yaml.

LT-06 root cause: the original contract had only ``projection_api.snapshots``
(reducer-format metadata) and lacked ``expose: true`` + ``exposures`` with
``table`` and ``columns`` required by the projection API discovery module.
This test pins the fix so it cannot regress.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.projection.discovery import build_projection_topic_map

_GATE_ACTIVITY_TOPIC = "onex.snapshot.projection.gate.activity.v1"
_GATE_METRICS_TOPIC = "onex.snapshot.projection.gate.metrics.v1"
_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_omnigate_projection"
    / "contract.yaml"
)


@pytest.mark.unit
def test_gate_activity_topic_is_in_projection_topic_map() -> None:
    """gate.activity topic must appear in the topic map built from installed contracts.

    Regression lock for LT-06: the contract previously lacked expose:true so
    the topic returned 404 from the projection API.
    """
    topic_map = build_projection_topic_map()
    assert _GATE_ACTIVITY_TOPIC in topic_map, (
        f"{_GATE_ACTIVITY_TOPIC!r} not found in projection topic map. "
        "The node_omnigate_projection contract must declare "
        "projection_api.expose: true with an exposures entry for this topic."
    )


@pytest.mark.unit
def test_gate_metrics_topic_is_in_projection_topic_map() -> None:
    """gate.metrics topic must appear in the topic map built from installed contracts.

    Regression lock for LT-06: the contract previously lacked expose:true so
    the topic returned 404 from the projection API.
    """
    topic_map = build_projection_topic_map()
    assert _GATE_METRICS_TOPIC in topic_map, (
        f"{_GATE_METRICS_TOPIC!r} not found in projection topic map. "
        "The node_omnigate_projection contract must declare "
        "projection_api.expose: true with an exposures entry for this topic."
    )


@pytest.mark.unit
def test_gate_activity_config_has_required_fields() -> None:
    """gate.activity config must have table, columns, schema populated."""
    topic_map = build_projection_topic_map()
    cfg = topic_map.get(_GATE_ACTIVITY_TOPIC)
    assert cfg is not None, f"{_GATE_ACTIVITY_TOPIC!r} not in topic map"
    assert cfg.table == "gate_activity"
    assert cfg.schema_name == "public"
    assert len(cfg.columns) > 0
    assert "status" in cfg.columns
    assert "repository_id" in cfg.columns
    assert "observed_at" in cfg.columns


@pytest.mark.unit
def test_gate_metrics_config_has_required_fields() -> None:
    """gate.metrics config must have table, columns, schema populated."""
    topic_map = build_projection_topic_map()
    cfg = topic_map.get(_GATE_METRICS_TOPIC)
    assert cfg is not None, f"{_GATE_METRICS_TOPIC!r} not in topic map"
    assert cfg.table == "gate_metrics"
    assert cfg.schema_name == "public"
    assert len(cfg.columns) > 0
    assert "total_events" in cfg.columns
    assert "passed" in cfg.columns
    assert "failed" in cfg.columns


@pytest.mark.unit
def test_gate_projection_contract_file_exists() -> None:
    """Confirm the contract file is present at the expected path."""
    assert _CONTRACT_PATH.exists(), (
        f"node_omnigate_projection/contract.yaml not found at {_CONTRACT_PATH}"
    )
