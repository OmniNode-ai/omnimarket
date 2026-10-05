# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Characterization tests for the platform readiness kafka_topic_coverage dimension.

The dimension lists the broker's topics over ssh and reports every entry of
``SOW_PHASE2_REQUIRED_TOPICS`` that the broker does not have. These tests pin that
behaviour against whatever the list holds, so a change to the list's entries does
not change what the dimension does with them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omnimarket.nodes.node_platform_readiness.handlers.handler_platform_readiness import (
    SOW_PHASE2_REQUIRED_TOPICS,
    NodePlatformReadiness,
)

_TARGET = "user@testhost.example"
_NOW = datetime(2026, 10, 5, tzinfo=UTC)


class _RpkTopicList:
    """subprocess.run stand-in that answers ``rpk topic list`` with fixed topics."""

    def __init__(self, topics: list[str], returncode: int = 0) -> None:
        self.topics = topics
        self.returncode = returncode
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        self.calls.append(list(argv))
        stdout = "NAME  PARTITIONS  REPLICAS\n" + "".join(
            f"{topic}  1  1\n" for topic in self.topics
        )

        return SimpleNamespace(
            returncode=self.returncode, stdout=stdout, stderr="broker unreachable"
        )


@pytest.mark.unit
class TestKafkaTopicCoverage:
    def test_all_required_topics_present_is_healthy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = _RpkTopicList([*SOW_PHASE2_REQUIRED_TOPICS, "onex.evt.other.v1"])
        monkeypatch.setattr("subprocess.run", run)

        dim = NodePlatformReadiness()._check_kafka_topic_coverage(_NOW, _TARGET)

        assert dim.name == "kafka_topic_coverage"
        assert dim.critical is False
        assert dim.healthy is True
        assert dim.details == "All SOW Phase 2 topics present"
        assert run.calls[0][:2] == ["ssh", _TARGET]
        assert "rpk topic list" in run.calls[0][2]

    def test_missing_topic_is_named_in_details(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        missing = SOW_PHASE2_REQUIRED_TOPICS[0]
        present = [t for t in SOW_PHASE2_REQUIRED_TOPICS if t != missing]
        monkeypatch.setattr("subprocess.run", _RpkTopicList(present))

        dim = NodePlatformReadiness()._check_kafka_topic_coverage(_NOW, _TARGET)

        assert dim.healthy is False
        assert dim.details == f"Missing topics: {missing}"

    def test_rpk_failure_is_unknown_not_unhealthy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("subprocess.run", _RpkTopicList([], returncode=1))

        dim = NodePlatformReadiness()._check_kafka_topic_coverage(_NOW, _TARGET)

        assert dim.healthy is None
        assert dim.details == "Kafka topic check failed (exit 1): broker unreachable"

    def test_required_topics_are_unique_onex_event_topics(self) -> None:
        assert len(set(SOW_PHASE2_REQUIRED_TOPICS)) == len(SOW_PHASE2_REQUIRED_TOPICS)
        for topic in SOW_PHASE2_REQUIRED_TOPICS:
            assert topic.startswith("onex.evt.")
            assert topic.endswith(".v1")

    def test_required_topics_are_declared_by_a_contract(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        contracts = [
            path.read_text()
            for path in (repo_root / "src/omnimarket/nodes").glob("*/contract.yaml")
        ]
        for topic in SOW_PHASE2_REQUIRED_TOPICS:
            assert any(topic in contract for contract in contracts), topic


@pytest.mark.unit
class TestTopicNamingBaselineIsOnlyTheForeignTopic:
    def test_baseline_contains_only_the_foreign_topic(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        baseline = repo_root / "scripts/validation/topic_naming_baseline.txt"
        topics = [
            line
            for line in baseline.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        ]
        assert topics == ["onex.tenant.events"]
