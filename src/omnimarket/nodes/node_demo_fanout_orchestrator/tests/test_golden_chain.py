# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_demo_fanout_orchestrator [OMN-12235].

Verifies contract YAML is valid, handler imports cleanly, and models are
well-formed. Dry-run execution uses deterministic provider fixtures.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

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
        assert data["name"] == "node_demo_fanout_orchestrator"
        assert data["node_type"] == "orchestrator"
        assert data.get("node_not_implemented") is False

    def test_contract_declares_handler(self, contract_path: Path) -> None:
        import yaml

        with open(contract_path) as f:
            data = yaml.safe_load(f)
        handler = data.get("handler", {})
        assert "module" in handler
        assert "class" in handler

    def test_contract_has_topics(self, contract_path: Path) -> None:
        import yaml

        with open(contract_path) as f:
            data = yaml.safe_load(f)
        eb = data.get("event_bus", {})
        assert eb.get("subscribe_topics")
        assert eb.get("publish_topics")


class TestMetadataYaml:
    def test_metadata_exists(self, metadata_path: Path) -> None:
        assert metadata_path.exists()

    def test_metadata_loads(self, metadata_path: Path) -> None:
        import yaml

        with open(metadata_path) as f:
            data = yaml.safe_load(f)
        assert data["name"] == "node_demo_fanout_orchestrator"
        assert "version" in data
        assert "entry_points" in data


class TestHandlerImport:
    def test_handler_module_imports(self) -> None:
        from omnimarket.nodes.node_demo_fanout_orchestrator.handlers import (
            handler_fanout,
        )

        assert handler_fanout is not None

    def test_handler_class_exists(self) -> None:
        from omnimarket.nodes.node_demo_fanout_orchestrator.handlers.handler_fanout import (
            HandlerDemoFanoutOrchestrator,
        )

        assert HandlerDemoFanoutOrchestrator is not None

    def test_input_model_exists(self) -> None:
        from omnimarket.nodes.node_demo_fanout_orchestrator.models.model_fanout_request import (
            ModelDemoFanoutRequest,
        )

        assert ModelDemoFanoutRequest is not None


class TestModels:
    def test_model_config_frozen(self) -> None:
        from pydantic import ValidationError

        from omnimarket.nodes.node_demo_fanout_orchestrator.models.model_fanout_request import (
            ModelDemoModelConfig,
        )

        cfg = ModelDemoModelConfig(
            model_id="qwen3-coder-30b", endpoint_url="http://localhost:8000"
        )
        with pytest.raises(ValidationError):
            cfg.model_id = "mutated"  # type: ignore[misc]

    def test_fanout_request_model(self) -> None:
        from omnimarket.nodes.node_demo_fanout_orchestrator.models.model_fanout_request import (
            ModelDemoFanoutRequest,
            ModelDemoModelConfig,
        )

        req = ModelDemoFanoutRequest(
            run_id=uuid4(),
            correlation_id=uuid4(),
            tasks=["hello world"],
            model_configs=[
                ModelDemoModelConfig(
                    model_id="m1", endpoint_url="http://localhost:8000"
                )
            ],
        )
        assert len(req.tasks) == 1
        assert len(req.model_configs) == 1


class TestHandlerBehavior:
    @pytest.mark.asyncio
    async def test_handle_runs_dry_run_provider_fixtures(self) -> None:
        from omnimarket.nodes.node_demo_fanout_orchestrator.handlers.handler_fanout import (
            HandlerDemoFanoutOrchestrator,
        )
        from omnimarket.nodes.node_demo_fanout_orchestrator.models.model_fanout_request import (
            ModelDemoFanoutRequest,
            ModelDemoModelConfig,
            ModelDemoProviderFixture,
        )

        handler = HandlerDemoFanoutOrchestrator()
        req = ModelDemoFanoutRequest(
            run_id=uuid4(),
            correlation_id=uuid4(),
            tasks=["ping"],
            model_configs=[
                ModelDemoModelConfig(
                    model_id="m1",
                    endpoint_url="fixture://m1",
                    provider="deterministic_fixture",
                )
            ],
            dry_run=True,
            provider_fixtures={
                "m1": ModelDemoProviderFixture(
                    outputs=["pong"], prompt_tokens=7, completion_tokens=3
                )
            },
        )
        result = await handler.handle(req)

        assert len(result.results) == 1
        assert result.results[0].output_text == "pong"
        assert result.results[0].prompt_tokens == 7
        assert result.results[0].completion_tokens == 3

    @pytest.mark.asyncio
    async def test_live_provider_missing_credentials_preflights(self) -> None:
        from omnimarket.nodes.node_demo_fanout_orchestrator.handlers.handler_fanout import (
            HandlerDemoFanoutOrchestrator,
        )
        from omnimarket.nodes.node_demo_fanout_orchestrator.models.model_fanout_request import (
            ModelDemoFanoutRequest,
            ModelDemoModelConfig,
        )

        handler = HandlerDemoFanoutOrchestrator()
        req = ModelDemoFanoutRequest(
            run_id=uuid4(),
            correlation_id=uuid4(),
            tasks=["ping"],
            model_configs=[
                ModelDemoModelConfig(
                    model_id="gemini/gemini-2.0-flash",
                    endpoint_url="https://generativelanguage.googleapis.com/v1beta/openai",
                    provider="openai_compatible",
                    api_key_env_var="ONEX_DEMO_TEST_MISSING_KEY",
                )
            ],
        )

        with pytest.raises(RuntimeError, match="ONEX_DEMO_TEST_MISSING_KEY"):
            await handler.handle(req)
