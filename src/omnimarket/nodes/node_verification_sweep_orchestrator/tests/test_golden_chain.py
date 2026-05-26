# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Golden chain test for node_verification_sweep_orchestrator.

Verifies OMN-12223: the verification_sweep_orchestrator node can be loaded and
its contract + handler structure is correct. The handler raises NotImplementedError
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
    """Path to the node_verification_sweep_orchestrator directory."""
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

    def test_contract_node_type_is_orchestrator(self, contract_path: Path) -> None:
        with open(contract_path) as f:
            data = yaml.safe_load(f)
        assert data.get("node_type") == "orchestrator"

    def test_contract_is_marked_not_implemented(self, contract_path: Path) -> None:
        with open(contract_path) as f:
            data = yaml.safe_load(f)
        assert data.get("node_not_implemented") is True

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

    def test_contract_descriptor_is_orchestrator(self, contract_path: Path) -> None:
        with open(contract_path) as f:
            data = yaml.safe_load(f)
        descriptor = data.get("descriptor", {})
        assert descriptor.get("node_archetype") == "orchestrator"


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

    def test_metadata_node_role_is_orchestrator(self, metadata_path: Path) -> None:
        with open(metadata_path) as f:
            data = yaml.safe_load(f)
        assert data.get("node_role") == "orchestrator"


class TestHandlerImport:
    """Handler and model modules can be imported and classes exist."""

    def test_handler_module_imports(self) -> None:
        from omnimarket.nodes.node_verification_sweep_orchestrator.handlers import (
            handler_verification_sweep_orchestrator,
        )

        assert handler_verification_sweep_orchestrator is not None

    def test_handler_class_exists(self) -> None:
        from omnimarket.nodes.node_verification_sweep_orchestrator.handlers.handler_verification_sweep_orchestrator import (
            HandlerVerificationSweepOrchestrator,
        )

        assert HandlerVerificationSweepOrchestrator is not None

    def test_request_model_importable(self) -> None:
        from omnimarket.nodes.node_verification_sweep_orchestrator.models.model_verification_sweep_orchestrator_request import (
            ModelVerificationSweepOrchestratorRequest,
        )

        assert ModelVerificationSweepOrchestratorRequest is not None

    def test_result_model_importable(self) -> None:
        from omnimarket.nodes.node_verification_sweep_orchestrator.models.model_verification_sweep_orchestrator_result import (
            ModelVerificationSweepOrchestratorResult,
        )

        assert ModelVerificationSweepOrchestratorResult is not None

    def test_endpoint_result_model_importable(self) -> None:
        from omnimarket.nodes.node_verification_sweep_orchestrator.models.model_verification_sweep_orchestrator_result import (
            ModelEndpointVerificationResult,
        )

        assert ModelEndpointVerificationResult is not None

    def test_database_result_model_importable(self) -> None:
        from omnimarket.nodes.node_verification_sweep_orchestrator.models.model_verification_sweep_orchestrator_result import (
            ModelDatabaseVerificationResult,
        )

        assert ModelDatabaseVerificationResult is not None

    def test_dod_result_model_importable(self) -> None:
        from omnimarket.nodes.node_verification_sweep_orchestrator.models.model_verification_sweep_orchestrator_result import (
            ModelDodEvidenceVerificationResult,
        )

        assert ModelDodEvidenceVerificationResult is not None


class TestHandlerStubBehaviour:
    """Handler raises NotImplementedError as documented by node_not_implemented: true."""

    def test_handler_raises_not_implemented(self) -> None:
        from omnimarket.nodes.node_verification_sweep_orchestrator.handlers.handler_verification_sweep_orchestrator import (
            HandlerVerificationSweepOrchestrator,
        )
        from omnimarket.nodes.node_verification_sweep_orchestrator.models.model_verification_sweep_orchestrator_request import (
            ModelVerificationSweepOrchestratorRequest,
        )

        handler = HandlerVerificationSweepOrchestrator()
        request = ModelVerificationSweepOrchestratorRequest()
        with pytest.raises(NotImplementedError):
            handler.handle(request)

    def test_request_model_defaults(self) -> None:
        from omnimarket.nodes.node_verification_sweep_orchestrator.models.model_verification_sweep_orchestrator_request import (
            ModelVerificationSweepOrchestratorRequest,
        )

        req = ModelVerificationSweepOrchestratorRequest()
        assert req.targets == []
        assert req.epic is None
        assert req.check_types == []
        assert req.dry_run is False
        assert req.pr is None
        assert req.timeout_seconds == 30

    def test_result_model_defaults(self) -> None:
        from omnimarket.nodes.node_verification_sweep_orchestrator.models.model_verification_sweep_orchestrator_result import (
            ModelVerificationSweepOrchestratorResult,
        )

        result = ModelVerificationSweepOrchestratorResult()
        assert result.endpoint_results == []
        assert result.db_checks == []
        assert result.dod_receipts == []
        assert result.overall_status == "skip"
        assert result.receipt_path == ""
        assert result.dry_run is False
