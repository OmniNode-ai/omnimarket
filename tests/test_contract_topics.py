# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for contract-driven topic discovery."""

from __future__ import annotations

from omnimarket.runtime.contract_topics import (
    canonical_topic_to_dispatch_alias,
    collect_publish_topics_for_dispatch,
    collect_subscribe_topics_from_contracts,
)


def test_collect_subscribe_topics() -> None:
    """Subscribe topics are collected from contract.yaml files."""
    topics = collect_subscribe_topics_from_contracts()
    assert isinstance(topics, list)
    assert len(topics) > 0
    for topic in topics:
        assert isinstance(topic, str)
        assert "subscribe" not in topic  # should be actual topic names


def test_collect_publish_topics() -> None:
    """Publish topics are collected and keyed by dispatch name."""
    topics = collect_publish_topics_for_dispatch()
    assert isinstance(topics, dict)
    assert len(topics) > 0
    for key, topic in topics.items():
        assert isinstance(key, str)
        assert isinstance(topic, str)


def test_canonical_topic_to_dispatch_alias_cmd() -> None:
    result = canonical_topic_to_dispatch_alias(
        "onex.cmd.omnimarket.merge-sweep-start.v1"
    )
    assert result == "onex.commands.omnimarket.merge-sweep-start.v1"


def test_canonical_topic_to_dispatch_alias_evt() -> None:
    result = canonical_topic_to_dispatch_alias(
        "onex.evt.omnimarket.merge-sweep-completed.v1"
    )
    assert result == "onex.events.omnimarket.merge-sweep-completed.v1"


def test_canonical_topic_to_dispatch_alias_no_match() -> None:
    """Topics without .cmd. or .evt. pass through unchanged."""
    result = canonical_topic_to_dispatch_alias("some.other.topic")
    assert result == "some.other.topic"


def test_collect_subscribe_topics_with_override() -> None:
    """Override package list works."""
    topics = collect_subscribe_topics_from_contracts(
        node_packages=["omnimarket.nodes.node_merge_sweep"]
    )
    assert len(topics) == 1
    assert topics[0] == "onex.cmd.omnimarket.merge-sweep-start.v1"


def test_collect_subscribe_topics_missing_package() -> None:
    """Missing packages are skipped gracefully."""
    topics = collect_subscribe_topics_from_contracts(
        node_packages=["nonexistent.package.foo"]
    )
    assert topics == []
