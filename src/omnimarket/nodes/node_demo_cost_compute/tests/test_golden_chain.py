# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_demo_cost_compute [OMN-12235].

Verifies contract YAML is valid, handler imports cleanly, models are
well-formed. Handler execution raises NotImplementedError (stub-ok).
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def node_dir() -> Path:
    return Path(__file__).resolve().parent.parent


@pytest.fixture
def contract_path(node_dir: Path) -> Path:
    return node_dir / "contract.yaml"


@pytest.fixture
def metadata_path(node_dir: Path) -> Path:
    return node_dir / "metadata.yaml"


class TestContractYaml:
    def test_contract_exists(self, contract_path: Path) -> None:
        assert contract_path.exists()

    def test_contract_loads(self, contract_path: Path) -> None:
        import yaml

        with open(contract_path) as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)
        assert data["name"] == "node_demo_cost_compute"
        assert data["node_type"] == "compute"
        assert data.get("node_not_implemented") is True

    def test_contract_declares_handler(self, contract_path: Path) -> None:
        import yaml

        with open(contract_path) as f:
            data = yaml.safe_load(f)
        handler = data.get("handler", {})
        assert "module" in handler
        assert "class" in handler

    def test_contract_purity(self, contract_path: Path) -> None:
        import yaml

        with open(contract_path) as f:
            data = yaml.safe_load(f)
        descriptor = data.get("descriptor", {})
        assert descriptor.get("purity") == "pure"
        assert descriptor.get("idempotent") is True


class TestMetadataYaml:
    def test_metadata_exists(self, metadata_path: Path) -> None:
        assert metadata_path.exists()

    def test_metadata_loads(self, metadata_path: Path) -> None:
        import yaml

        with open(metadata_path) as f:
            data = yaml.safe_load(f)
        assert data["name"] == "node_demo_cost_compute"
        assert "version" in data
        assert "entry_points" in data


class TestHandlerImport:
    def test_handler_module_imports(self) -> None:
        from omnimarket.nodes.node_demo_cost_compute.handlers import (
            handler_cost_compute,
        )

        assert handler_cost_compute is not None

    def test_handler_class_exists(self) -> None:
        from omnimarket.nodes.node_demo_cost_compute.handlers.handler_cost_compute import (
            NodeDemoCostCompute,
        )

        assert NodeDemoCostCompute is not None

    def test_input_model_exists(self) -> None:
        from omnimarket.nodes.node_demo_cost_compute.models.model_cost_request import (
            ModelDemoCostRequest,
        )

        assert ModelDemoCostRequest is not None


class TestModels:
    def test_pricing_model_frozen(self) -> None:
        from pydantic import ValidationError

        from omnimarket.nodes.node_demo_cost_compute.models.model_cost_request import (
            ModelDemoModelPricing,
        )

        pricing = ModelDemoModelPricing(
            prompt_cost_per_1k=0.001, completion_cost_per_1k=0.002
        )
        with pytest.raises(ValidationError):
            pricing.prompt_cost_per_1k = 99.0  # type: ignore[misc]

    def test_cost_request_model(self) -> None:
        from omnimarket.nodes.node_demo_cost_compute.models.model_cost_request import (
            ModelDemoCostRequest,
            ModelDemoModelPricing,
        )
        from omnimarket.nodes.node_demo_fanout_orchestrator.models.model_fanout_request import (
            ModelDemoInferenceResult,
        )

        req = ModelDemoCostRequest(
            inference_results=[
                ModelDemoInferenceResult(
                    model_id="m1",
                    prompt_tokens=100,
                    completion_tokens=50,
                    latency_ms=123.4,
                )
            ],
            pricing_table={
                "m1": ModelDemoModelPricing(
                    prompt_cost_per_1k=0.001, completion_cost_per_1k=0.002
                )
            },
        )
        assert len(req.inference_results) == 1
        assert "m1" in req.pricing_table


class TestHandlerIsStub:
    def test_handle_raises_not_implemented(self) -> None:
        from omnimarket.nodes.node_demo_cost_compute.handlers.handler_cost_compute import (
            NodeDemoCostCompute,
        )
        from omnimarket.nodes.node_demo_cost_compute.models.model_cost_request import (
            ModelDemoCostRequest,
            ModelDemoModelPricing,
        )
        from omnimarket.nodes.node_demo_fanout_orchestrator.models.model_fanout_request import (
            ModelDemoInferenceResult,
        )

        handler = NodeDemoCostCompute()
        req = ModelDemoCostRequest(
            inference_results=[
                ModelDemoInferenceResult(
                    model_id="m1",
                    prompt_tokens=10,
                    completion_tokens=5,
                    latency_ms=10.0,
                )
            ],
            pricing_table={
                "m1": ModelDemoModelPricing(
                    prompt_cost_per_1k=0.001, completion_cost_per_1k=0.002
                )
            },
        )
        with pytest.raises(NotImplementedError):
            handler.handle(req)
