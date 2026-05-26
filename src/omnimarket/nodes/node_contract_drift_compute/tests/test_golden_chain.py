# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden chain test for node_contract_drift_compute.

Verifies OMN-12222: the contract_drift_compute node can be loaded and its
contract + handler structure is correct. The handler raises NotImplementedError
(node_not_implemented: true) — the tests verify the stub contract, metadata,
import surface, and NotImplementedError behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def node_dir() -> Path:
    """Path to the node_contract_drift_compute directory."""
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def contract_path(node_dir: Path) -> Path:
    return node_dir / "contract.yaml"


@pytest.fixture
def metadata_path(node_dir: Path) -> Path:
    return node_dir / "metadata.yaml"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestContractYaml:
    """Contract YAML is valid and declares required fields."""

    def test_contract_exists(self, contract_path: Path) -> None:
        assert contract_path.exists(), f"contract.yaml not found at {contract_path}"

    def test_contract_loads(self, contract_path: Path) -> None:
        with open(contract_path) as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)
        assert "name" in data
        assert "contract_version" in data
        assert "handler" in data

    def test_contract_node_type_is_compute(self, contract_path: Path) -> None:
        with open(contract_path) as f:
            data = yaml.safe_load(f)
        assert data.get("node_type") == "compute"

    def test_contract_is_marked_not_implemented(self, contract_path: Path) -> None:
        with open(contract_path) as f:
            data = yaml.safe_load(f)
        assert data.get("node_not_implemented") is True

    def test_contract_purity_is_pure(self, contract_path: Path) -> None:
        with open(contract_path) as f:
            data = yaml.safe_load(f)
        descriptor = data.get("descriptor", {})
        assert descriptor.get("purity") == "pure"

    def test_contract_idempotent(self, contract_path: Path) -> None:
        with open(contract_path) as f:
            data = yaml.safe_load(f)
        descriptor = data.get("descriptor", {})
        assert descriptor.get("idempotent") is True

    def test_contract_declares_handler(self, contract_path: Path) -> None:
        with open(contract_path) as f:
            data = yaml.safe_load(f)
        handler = data.get("handler", {})
        assert "module" in handler, "handler.module not declared"
        assert "class" in handler, "handler.class not declared"
        assert "input_model" in handler, "handler.input_model not declared"

    def test_contract_declares_event_bus(self, contract_path: Path) -> None:
        with open(contract_path) as f:
            data = yaml.safe_load(f)
        event_bus = data.get("event_bus", {})
        assert "subscribe_topics" in event_bus
        assert "publish_topics" in event_bus
        assert len(event_bus["subscribe_topics"]) > 0
        assert len(event_bus["publish_topics"]) > 0

    def test_contract_topics_follow_naming_convention(
        self, contract_path: Path
    ) -> None:
        with open(contract_path) as f:
            data = yaml.safe_load(f)
        event_bus = data.get("event_bus", {})
        all_topics = event_bus.get("subscribe_topics", []) + event_bus.get(
            "publish_topics", []
        )
        for topic in all_topics:
            assert topic.startswith("onex."), (
                f"Topic {topic!r} does not start with 'onex.'"
            )


class TestMetadataYaml:
    """Metadata YAML is valid and has required fields."""

    def test_metadata_exists(self, metadata_path: Path) -> None:
        assert metadata_path.exists(), f"metadata.yaml not found at {metadata_path}"

    def test_metadata_loads(self, metadata_path: Path) -> None:
        with open(metadata_path) as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)
        assert "name" in data
        assert "version" in data
        assert "entry_points" in data

    def test_metadata_node_role_is_compute(self, metadata_path: Path) -> None:
        with open(metadata_path) as f:
            data = yaml.safe_load(f)
        assert data.get("node_role") == "compute"


class TestHandlerImport:
    """Handler module can be imported and class/models exist."""

    def test_handler_module_imports(self) -> None:
        from omnimarket.nodes.node_contract_drift_compute.handlers import (
            handler_contract_drift_compute,
        )

        assert handler_contract_drift_compute is not None

    def test_handler_class_exists(self) -> None:
        from omnimarket.nodes.node_contract_drift_compute.handlers.handler_contract_drift_compute import (
            NodeContractDriftCompute,
        )

        assert NodeContractDriftCompute is not None

    def test_input_model_exists(self) -> None:
        from omnimarket.nodes.node_contract_drift_compute.handlers.handler_contract_drift_compute import (
            ModelContractDriftComputeRequest,
        )

        assert ModelContractDriftComputeRequest is not None

    def test_output_model_exists(self) -> None:
        from omnimarket.nodes.node_contract_drift_compute.handlers.handler_contract_drift_compute import (
            ModelContractDriftComputeResult,
        )

        assert ModelContractDriftComputeResult is not None

    def test_finding_model_exists(self) -> None:
        from omnimarket.nodes.node_contract_drift_compute.handlers.handler_contract_drift_compute import (
            ModelContractDriftFinding,
        )

        assert ModelContractDriftFinding is not None

    def test_boundary_finding_model_exists(self) -> None:
        from omnimarket.nodes.node_contract_drift_compute.handlers.handler_contract_drift_compute import (
            ModelBoundaryFinding,
        )

        assert ModelBoundaryFinding is not None


class TestHandlerStubBehaviour:
    """Handler raises NotImplementedError as documented by node_not_implemented: true."""

    def test_handler_raises_not_implemented(self) -> None:
        from omnimarket.nodes.node_contract_drift_compute.handlers.handler_contract_drift_compute import (
            ModelContractDriftComputeRequest,
            NodeContractDriftCompute,
        )

        handler = NodeContractDriftCompute()
        request = ModelContractDriftComputeRequest()
        with pytest.raises(NotImplementedError):
            handler.handle(request)

    def test_request_model_defaults(self) -> None:
        from omnimarket.nodes.node_contract_drift_compute.handlers.handler_contract_drift_compute import (
            ModelContractDriftComputeRequest,
        )

        req = ModelContractDriftComputeRequest()
        assert req.repos == []
        assert req.baseline_path == ""
        assert req.dry_run is False
        assert req.sensitivity == "STANDARD"
        assert req.severity_threshold == "BREAKING"
        assert req.check_boundaries is True

    def test_result_model_defaults(self) -> None:
        from omnimarket.nodes.node_contract_drift_compute.handlers.handler_contract_drift_compute import (
            ModelContractDriftComputeResult,
        )

        result = ModelContractDriftComputeResult()
        assert result.drifted_contracts == []
        assert result.boundary_findings == []
        assert result.staleness_scores == {}
        assert result.violations == []
        assert result.overall_status == "clean"
        assert result.repos_scanned == 0
        assert result.total_contracts_checked == 0
