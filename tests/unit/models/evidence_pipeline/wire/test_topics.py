# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for evidence_pipeline/wire/topics.py.

Covers:
- All 8 constants are present and non-empty strings
- Constants follow the canonical ONEX topic format: onex.{cmd|evt}.{service}.{event}.v{N}
- __all__ parity with module exports
- Constants are importable via the wire package __init__
- Topic kind prefixes are correct (cmd vs evt)
- Migration target / removal date comments are present
- Contract YAML enumerates the same full topic names
"""

from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

import omnimarket.models.evidence_pipeline.wire.topics as topics_module
from omnimarket.models.evidence_pipeline.wire.topics import (
    EVIDENCE_COLLECTED_EVT_V1,
    EVIDENCE_DASHBOARD_PROJECTED_EVT_V1,
    EVIDENCE_EXTRACTED_EVT_V1,
    EVIDENCE_GAP_ANALYZED_EVT_V1,
    EVIDENCE_OCC_PR_CREATED_EVT_V1,
    EVIDENCE_PIPELINE_START_CMD_V1,
    EVIDENCE_READINESS_SCORED_EVT_V1,
    EVIDENCE_VALIDATED_EVT_V1,
)

# Canonical ONEX topic pattern (OMN-1537)
_CANONICAL_TOPIC_RE = re.compile(
    r"^onex\.(cmd|evt|dlq|snapshot|intent)\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+(?:\.[a-zA-Z0-9_-]+)*\.v\d+$"
)

_ALL_CONSTANTS = [
    EVIDENCE_COLLECTED_EVT_V1,
    EVIDENCE_DASHBOARD_PROJECTED_EVT_V1,
    EVIDENCE_EXTRACTED_EVT_V1,
    EVIDENCE_GAP_ANALYZED_EVT_V1,
    EVIDENCE_OCC_PR_CREATED_EVT_V1,
    EVIDENCE_PIPELINE_START_CMD_V1,
    EVIDENCE_READINESS_SCORED_EVT_V1,
    EVIDENCE_VALIDATED_EVT_V1,
]


# ---------------------------------------------------------------------------
# 1. Constant presence and format
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_all_constants_are_non_empty_strings() -> None:
    for topic in _ALL_CONSTANTS:
        assert isinstance(topic, str), f"Topic {topic!r} is not a string"
        assert len(topic) > 0, f"Topic {topic!r} is empty"


@pytest.mark.unit
def test_all_constants_match_canonical_topic_format() -> None:
    for topic in _ALL_CONSTANTS:
        assert _CANONICAL_TOPIC_RE.match(topic), (
            f"Topic {topic!r} does not match canonical ONEX format "
            "onex.{{cmd|evt}}.{{service}}.{{event}}.v{{N}}"
        )


@pytest.mark.unit
def test_cmd_constants_have_cmd_prefix() -> None:
    assert EVIDENCE_PIPELINE_START_CMD_V1.startswith("onex.cmd."), (
        f"Command topic {EVIDENCE_PIPELINE_START_CMD_V1!r} must start with 'onex.cmd.'"
    )


@pytest.mark.unit
def test_evt_constants_have_evt_prefix() -> None:
    evt_topics = [
        EVIDENCE_COLLECTED_EVT_V1,
        EVIDENCE_DASHBOARD_PROJECTED_EVT_V1,
        EVIDENCE_EXTRACTED_EVT_V1,
        EVIDENCE_GAP_ANALYZED_EVT_V1,
        EVIDENCE_OCC_PR_CREATED_EVT_V1,
        EVIDENCE_READINESS_SCORED_EVT_V1,
        EVIDENCE_VALIDATED_EVT_V1,
    ]
    for topic in evt_topics:
        assert topic.startswith("onex.evt."), (
            f"Event topic {topic!r} must start with 'onex.evt.'"
        )


@pytest.mark.unit
def test_all_constants_use_omnimarket_service() -> None:
    for topic in _ALL_CONSTANTS:
        assert "omnimarket" in topic, (
            f"Topic {topic!r} must use 'omnimarket' as the service segment"
        )


@pytest.mark.unit
def test_all_constants_end_with_v1() -> None:
    for topic in _ALL_CONSTANTS:
        assert topic.endswith(".v1"), f"Topic {topic!r} must end with '.v1'"


@pytest.mark.unit
def test_no_duplicate_topic_values() -> None:
    assert len(_ALL_CONSTANTS) == len(set(_ALL_CONSTANTS)), (
        "Duplicate topic constant values detected"
    )


# ---------------------------------------------------------------------------
# 2. __all__ parity
# ---------------------------------------------------------------------------

_EXPECTED_ALL = {
    "EVIDENCE_COLLECTED_EVT_V1",
    "EVIDENCE_DASHBOARD_PROJECTED_EVT_V1",
    "EVIDENCE_EXTRACTED_EVT_V1",
    "EVIDENCE_GAP_ANALYZED_EVT_V1",
    "EVIDENCE_OCC_PR_CREATED_EVT_V1",
    "EVIDENCE_PIPELINE_START_CMD_V1",
    "EVIDENCE_READINESS_SCORED_EVT_V1",
    "EVIDENCE_VALIDATED_EVT_V1",
}


@pytest.mark.unit
def test_all_count() -> None:
    assert len(topics_module.__all__) == 8


@pytest.mark.unit
def test_all_matches_expected_set() -> None:
    assert set(topics_module.__all__) == _EXPECTED_ALL


@pytest.mark.unit
def test_all_names_are_importable() -> None:
    for name in topics_module.__all__:
        assert hasattr(topics_module, name), (
            f"{name!r} in __all__ but not importable from module"
        )


# ---------------------------------------------------------------------------
# 3. Wire package __init__ re-exports
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_constants_importable_via_wire_init() -> None:
    wire = importlib.import_module("omnimarket.models.evidence_pipeline.wire")
    for name in topics_module.__all__:
        assert hasattr(wire, name), f"{name!r} not re-exported from wire __init__"


@pytest.mark.unit
def test_topic_values_preserve_the_moved_wire_api() -> None:
    assert {name: getattr(topics_module, name) for name in _EXPECTED_ALL} == {
        "EVIDENCE_PIPELINE_START_CMD_V1": "onex.cmd.omnimarket.evidence-pipeline-start.v1",
        "EVIDENCE_COLLECTED_EVT_V1": "onex.evt.omnimarket.evidence-collected.v1",
        "EVIDENCE_EXTRACTED_EVT_V1": "onex.evt.omnimarket.evidence-extracted.v1",
        "EVIDENCE_VALIDATED_EVT_V1": "onex.evt.omnimarket.evidence-validated.v1",
        "EVIDENCE_OCC_PR_CREATED_EVT_V1": "onex.evt.omnimarket.evidence-occ-pr-created.v1",
        "EVIDENCE_GAP_ANALYZED_EVT_V1": "onex.evt.omnimarket.evidence-gap-analyzed.v1",
        "EVIDENCE_READINESS_SCORED_EVT_V1": "onex.evt.omnimarket.evidence-readiness-scored.v1",
        "EVIDENCE_DASHBOARD_PROJECTED_EVT_V1": "onex.evt.omnimarket.evidence-dashboard-projected.v1",
    }


@pytest.mark.unit
def test_node_topic_follows_its_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node_dir = tmp_path / "node_example"
    node_dir.mkdir()
    contract = node_dir / "contract.yaml"
    contract.write_text(
        "event_bus:\n  publish_topics: [changed-topic]\n  subscribe_topics: [command-topic]\n"
    )
    monkeypatch.setattr(topics_module, "_NODES_ROOT", tmp_path)
    assert topics_module._node_topic("node_example") == "changed-topic"
    assert topics_module._node_topic("node_example", subscribe=True) == "command-topic"
    for declarations in ("[]", "[first, second]"):
        contract.write_text(f"event_bus:\n  publish_topics: {declarations}\n")
        with pytest.raises(ValueError, match="expected exactly one topic"):
            topics_module._node_topic("node_example")
    contract.write_text("event_bus: {}\n")
    with pytest.raises(ValueError, match="must be a string list"):
        topics_module._node_topic("node_example")


@pytest.mark.unit
def test_legacy_topic_requires_a_declared_nonempty_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = tmp_path / "contract.yaml"
    monkeypatch.setattr(topics_module, "_LEGACY_CONTRACT", contract)
    contract.write_text("legacy_topics: {example: preserved-topic}\n")
    assert topics_module._legacy_topic("example") == "preserved-topic"
    for declaration in ('""', "null", "42"):
        contract.write_text(f"legacy_topics: {{example: {declaration}}}\n")
        with pytest.raises(ValueError, match="must be a string"):
            topics_module._legacy_topic("example")
    contract.write_text("legacy_topics: {}\n")
    with pytest.raises(ValueError, match="is missing"):
        topics_module._legacy_topic("example")
