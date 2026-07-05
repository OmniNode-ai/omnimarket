# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for node_version_skew_detector — version compatibility checks."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
    ProtocolEventBusPublisher,
)

from omnimarket.nodes.node_version_skew_detector.handlers.handler_version_skew_detector import (
    NodeVersionInfo,
    NodeVersionSkewDetector,
    VersionSkewCheckRequest,
)

NODE_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture
def handler() -> NodeVersionSkewDetector:
    return NodeVersionSkewDetector(
        event_bus=cast(ProtocolEventBusPublisher, EventBusInmemory())
    )


class TestVersionMatchNoSkew:
    def test_matching_major_versions_no_nodes(
        self, handler: NodeVersionSkewDetector
    ) -> None:
        request = VersionSkewCheckRequest(
            plugin_version="1.2.3",
            runtime_version="1.5.0",
            installed_nodes=[],
        )
        result = handler.handle(request)
        assert result.status == "healthy"
        assert result.incompatible_nodes == []

    def test_matching_versions_with_compatible_nodes(
        self, handler: NodeVersionSkewDetector
    ) -> None:
        request = VersionSkewCheckRequest(
            plugin_version="1.2.3",
            runtime_version="1.5.0",
            installed_nodes=[
                NodeVersionInfo(name="node_a", version="1.0.0"),
                NodeVersionInfo(name="node_b", version="1.3.2"),
            ],
            runtime_compat_range=">=1.0.0,<2.0.0",
        )
        result = handler.handle(request)
        assert result.status == "healthy"
        assert result.incompatible_nodes == []


class TestVersionMismatchSkewDetected:
    def test_major_version_mismatch(self, handler: NodeVersionSkewDetector) -> None:
        request = VersionSkewCheckRequest(
            plugin_version="2.0.0",
            runtime_version="1.5.0",
            installed_nodes=[],
        )
        result = handler.handle(request)
        assert result.status == "skew_detected"
        assert any(n.name == "plugin" for n in result.incompatible_nodes)

    def test_skew_result_includes_versions(
        self, handler: NodeVersionSkewDetector
    ) -> None:
        request = VersionSkewCheckRequest(
            plugin_version="3.1.0",
            runtime_version="1.2.0",
            installed_nodes=[],
        )
        result = handler.handle(request)
        assert result.plugin_version == "3.1.0"
        assert result.runtime_version == "1.2.0"
        assert result.detected_at


class TestNodeCompatibility:
    def test_incompatible_node_detected(self, handler: NodeVersionSkewDetector) -> None:
        request = VersionSkewCheckRequest(
            plugin_version="1.2.3",
            runtime_version="1.5.0",
            installed_nodes=[
                NodeVersionInfo(name="node_good", version="1.0.0"),
                NodeVersionInfo(name="node_bad", version="2.5.0"),
            ],
            runtime_compat_range=">=1.0.0,<2.0.0",
        )
        result = handler.handle(request)
        assert result.status == "skew_detected"
        bad_nodes = [n for n in result.incompatible_nodes if n.name == "node_bad"]
        assert len(bad_nodes) == 1
        assert "outside runtime compatibility range" in bad_nodes[0].reason

    def test_all_nodes_compatible_empty_list(
        self, handler: NodeVersionSkewDetector
    ) -> None:
        request = VersionSkewCheckRequest(
            plugin_version="1.2.3",
            runtime_version="1.5.0",
            installed_nodes=[
                NodeVersionInfo(name="node_a", version="1.0.0"),
                NodeVersionInfo(name="node_b", version="1.9.9"),
            ],
            runtime_compat_range=">=1.0.0,<2.0.0",
        )
        result = handler.handle(request)
        assert result.status == "healthy"
        assert result.incompatible_nodes == []


class TestInvalidVersions:
    def test_invalid_plugin_version_detected(
        self, handler: NodeVersionSkewDetector
    ) -> None:
        request = VersionSkewCheckRequest(
            plugin_version="not-a-version",
            runtime_version="1.5.0",
            installed_nodes=[],
        )
        result = handler.handle(request)
        assert result.status == "skew_detected"
        assert any(
            "Invalid plugin version" in n.reason for n in result.incompatible_nodes
        )

    def test_invalid_node_version_detected(
        self, handler: NodeVersionSkewDetector
    ) -> None:
        request = VersionSkewCheckRequest(
            plugin_version="1.2.3",
            runtime_version="1.5.0",
            installed_nodes=[
                NodeVersionInfo(name="broken_node", version="abc"),
            ],
        )
        result = handler.handle(request)
        assert result.status == "skew_detected"
        assert any(n.name == "broken_node" for n in result.incompatible_nodes)


class TestContractYaml:
    def test_contract_exists(self) -> None:
        contract_path = NODE_DIR / "contract.yaml"
        assert contract_path.exists()

    def test_contract_loads(self) -> None:
        with open(NODE_DIR / "contract.yaml") as f:
            data = yaml.safe_load(f)
        assert data["name"] == "version_skew_detector"
        assert data["node_type"] == "compute"
        handler = data["handler"]
        assert "NodeVersionSkewDetector" in handler["class"]

    def test_contract_event_bus_topics(self) -> None:
        with open(NODE_DIR / "contract.yaml") as f:
            data = yaml.safe_load(f)
        event_bus = data["event_bus"]
        assert (
            "onex.cmd.omniclaude.version-skew-check.v1" in event_bus["subscribe_topics"]
        )
        assert (
            "onex.evt.omniclaude.version-skew-detected.v1"
            in event_bus["publish_topics"]
        )
