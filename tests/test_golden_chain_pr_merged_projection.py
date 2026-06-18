# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain contract tests for node_pr_merged_projection (OMN-13226 / T2).

The handler implementation is deferred to T3 (OMN-13227). This golden chain
verifies the contract stub itself: correct topic declarations, node_type, and
runtime_dispatch settings that the topic registry + drift gate depend on.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

CONTRACT_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_pr_merged_projection"
    / "contract.yaml"
)

EXPECTED_SUBSCRIBE_TOPIC = "onex.evt.github.pr-merged.v1"
EXPECTED_PUBLISH_TOPIC = "onex.evt.omnimarket.pr-merged-projection-snapshot.v1"


@pytest.mark.unit
class TestPrMergedProjectionContractGoldenChain:
    """Golden chain: contract declares canonical topic + correct runtime settings."""

    def _load_contract(self) -> dict[str, object]:
        return yaml.safe_load(CONTRACT_PATH.read_text())  # type: ignore[return-value]

    def test_contract_file_exists(self) -> None:
        """Contract YAML exists at the expected path."""
        assert CONTRACT_PATH.exists(), f"Missing contract at {CONTRACT_PATH}"

    def test_subscribe_topic_is_canonical(self) -> None:
        """subscribe_topics contains the canonical pr-merged topic."""
        contract = self._load_contract()
        event_bus = contract.get("event_bus") or {}
        assert isinstance(event_bus, dict)
        subscribe = event_bus.get("subscribe_topics") or []
        assert EXPECTED_SUBSCRIBE_TOPIC in subscribe, (
            f"Expected {EXPECTED_SUBSCRIBE_TOPIC!r} in subscribe_topics, got {subscribe}"
        )

    def test_publish_topic_is_canonical(self) -> None:
        """publish_topics contains the canonical projection-snapshot topic."""
        contract = self._load_contract()
        event_bus = contract.get("event_bus") or {}
        assert isinstance(event_bus, dict)
        publish = event_bus.get("publish_topics") or []
        assert EXPECTED_PUBLISH_TOPIC in publish, (
            f"Expected {EXPECTED_PUBLISH_TOPIC!r} in publish_topics, got {publish}"
        )

    def test_node_type_is_reducer(self) -> None:
        """node_type must be 'reducer' for an event-sourced projection."""
        contract = self._load_contract()
        assert contract.get("node_type") == "reducer"

    def test_runtime_dispatch_not_addressable(self) -> None:
        """runtime_dispatch.addressable is False — stub has no command route."""
        contract = self._load_contract()
        rd = contract.get("runtime_dispatch") or {}
        assert isinstance(rd, dict)
        assert rd.get("addressable") is False, (
            "Stub contract must declare addressable: false until T3 handler lands"
        )

    def test_handler_routing_declares_handler(self) -> None:
        """handler_routing names the future T3 handler (satisfies drift gate)."""
        contract = self._load_contract()
        hr = contract.get("handler_routing") or {}
        assert isinstance(hr, dict)
        handlers = hr.get("handlers") or []
        assert len(handlers) >= 1
        first = handlers[0]
        nested = first.get("handler") or {}
        assert nested.get("module"), "handler.module must be declared"
        assert nested.get("name"), "handler.name must be declared"

    def test_topics_use_canonical_naming(self) -> None:
        """All topics match the onex.{cmd|evt}.{service}.{name}.v1 convention."""
        contract = self._load_contract()
        event_bus = contract.get("event_bus") or {}
        assert isinstance(event_bus, dict)
        all_topics = list(event_bus.get("subscribe_topics") or []) + list(
            event_bus.get("publish_topics") or []
        )
        for topic in all_topics:
            assert isinstance(topic, str)
            parts = topic.split(".")
            assert parts[0] == "onex", f"Topic must start with 'onex': {topic}"
            assert parts[1] in ("cmd", "evt"), (
                f"Second segment must be 'cmd' or 'evt': {topic}"
            )
            assert parts[-1].startswith("v"), f"Topic must end with vN version: {topic}"
