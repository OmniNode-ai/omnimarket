# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the event-registry drift validator."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.validators.event_registry_drift import (
    compare_topic_sets,
    load_baseline,
    load_market_registry_topics,
    load_omniclaude_event_topics,
)


@pytest.mark.unit
def test_validator_flags_topicbase_reference_without_registry_binding(
    tmp_path: Path,
) -> None:
    topics_path = tmp_path / "topics.py"
    event_registry_path = tmp_path / "event_registry.py"
    registry_path = tmp_path / "topics.yaml"

    topics_path.write_text(
        "from enum import StrEnum\n"
        "class TopicBase(StrEnum):\n"
        '    SESSION_STARTED = "onex.evt.omniclaude.session-started.v1"\n'
        '    MISSING = "onex.evt.omniclaude.missing.v1"\n'
    )
    event_registry_path.write_text(
        "EVENT_REGISTRY = {\n"
        "    'session.started': EventRegistration(\n"
        "        fan_out=[FanOutRule(topic_base=TopicBase.SESSION_STARTED)]\n"
        "    ),\n"
        "    'missing': EventRegistration(\n"
        "        fan_out=[FanOutRule(topic_base=TopicBase.MISSING)]\n"
        "    ),\n"
        "}\n"
    )
    registry_path.write_text(
        "---\nevents:\n"
        "  session.started:\n"
        "    fan_out:\n"
        '      - topic: "onex.evt.omniclaude.session-started.v1"\n'
    )

    report = compare_topic_sets(
        source_topics=load_omniclaude_event_topics(event_registry_path, topics_path),
        registry_topics=load_market_registry_topics(registry_path),
    )

    assert report.source_only == frozenset({"onex.evt.omniclaude.missing.v1"})
    assert report.registry_only == frozenset()
    assert report.has_drift is True


@pytest.mark.unit
def test_validator_flags_registry_binding_without_topicbase_reference(
    tmp_path: Path,
) -> None:
    topics_path = tmp_path / "topics.py"
    event_registry_path = tmp_path / "event_registry.py"
    registry_path = tmp_path / "topics.yaml"

    topics_path.write_text(
        "from enum import StrEnum\n"
        "class TopicBase(StrEnum):\n"
        '    SESSION_STARTED = "onex.evt.omniclaude.session-started.v1"\n'
    )
    event_registry_path.write_text(
        "EVENT_REGISTRY = {\n"
        "    'session.started': EventRegistration(\n"
        "        fan_out=[FanOutRule(topic_base=TopicBase.SESSION_STARTED)]\n"
        "    ),\n"
        "}\n"
    )
    registry_path.write_text(
        "---\nevents:\n"
        "  session.started:\n"
        "    fan_out:\n"
        '      - topic: "onex.evt.omniclaude.session-started.v1"\n'
        "  extra.event:\n"
        "    fan_out:\n"
        '      - topic: "onex.evt.omniclaude.extra.v1"\n'
    )

    report = compare_topic_sets(
        source_topics=load_omniclaude_event_topics(event_registry_path, topics_path),
        registry_topics=load_market_registry_topics(registry_path),
    )

    assert report.source_only == frozenset()
    assert report.registry_only == frozenset({"onex.evt.omniclaude.extra.v1"})
    assert report.has_drift is True


@pytest.mark.unit
def test_baseline_suppresses_known_drift_but_not_new_drift(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.txt"
    baseline_path.write_text(
        "# existing debt\n"
        "source_only onex.evt.omniclaude.known-source-only.v1\n"
        "registry_only onex.evt.omniclaude.known-registry-only.v1\n"
    )

    baseline_source_only, baseline_registry_only = load_baseline(baseline_path)
    report = compare_topic_sets(
        source_topics={
            "onex.evt.omniclaude.shared.v1",
            "onex.evt.omniclaude.known-source-only.v1",
            "onex.evt.omniclaude.new-source-only.v1",
        },
        registry_topics={
            "onex.evt.omniclaude.shared.v1",
            "onex.evt.omniclaude.known-registry-only.v1",
            "onex.evt.omniclaude.new-registry-only.v1",
        },
        baseline_source_only=baseline_source_only,
        baseline_registry_only=baseline_registry_only,
    )

    assert report.baselined_source_only == frozenset(
        {"onex.evt.omniclaude.known-source-only.v1"}
    )
    assert report.baselined_registry_only == frozenset(
        {"onex.evt.omniclaude.known-registry-only.v1"}
    )
    assert report.source_only == frozenset({"onex.evt.omniclaude.new-source-only.v1"})
    assert report.registry_only == frozenset(
        {"onex.evt.omniclaude.new-registry-only.v1"}
    )
    assert report.has_drift is True
