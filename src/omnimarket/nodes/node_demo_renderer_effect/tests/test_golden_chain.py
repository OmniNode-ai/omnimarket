# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain test for node_demo_renderer_effect [OMN-12235].

Verifies contract YAML is valid, handler imports cleanly, models are
well-formed, and chart rendering is deterministic.
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
        assert data["name"] == "node_demo_renderer_effect"
        assert data["node_type"] == "EFFECT_GENERIC"
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
        assert data["name"] == "node_demo_renderer_effect"
        assert "version" in data
        assert "entry_points" in data


class TestHandlerImport:
    def test_handler_module_imports(self) -> None:
        from omnimarket.nodes.node_demo_renderer_effect.handlers import handler_renderer

        assert handler_renderer is not None

    def test_handler_class_exists(self) -> None:
        from omnimarket.nodes.node_demo_renderer_effect.handlers.handler_renderer import (
            NodeDemoRendererEffect,
        )

        assert NodeDemoRendererEffect is not None

    def test_input_model_exists(self) -> None:
        from omnimarket.nodes.node_demo_renderer_effect.models.model_render_request import (
            ModelDemoRenderRequest,
        )

        assert ModelDemoRenderRequest is not None


class TestModels:
    def test_render_request_frozen(self) -> None:
        from pydantic import ValidationError

        from omnimarket.events.demo import ModelDemoCostResult
        from omnimarket.nodes.node_demo_renderer_effect.models.model_render_request import (
            ModelDemoRenderRequest,
        )

        req = ModelDemoRenderRequest(
            cost_result=ModelDemoCostResult(costs=[], cheapest_model_id=None),
            bar_width=40,
        )
        with pytest.raises(ValidationError):
            req.bar_width = 99  # type: ignore[misc]

    def test_render_request_defaults(self) -> None:
        from omnimarket.events.demo import ModelDemoCostResult
        from omnimarket.nodes.node_demo_renderer_effect.models.model_render_request import (
            ModelDemoRenderRequest,
        )

        req = ModelDemoRenderRequest(
            cost_result=ModelDemoCostResult(costs=[], cheapest_model_id=None),
        )
        assert req.bar_width == 40
        assert req.title == "Model Cost Comparison"


class TestHandlerBehavior:
    def test_handle_renders_ascii_chart(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from omnimarket.events.demo import ModelDemoCostEntry, ModelDemoCostResult
        from omnimarket.nodes.node_demo_renderer_effect.handlers.handler_renderer import (
            NodeDemoRendererEffect,
        )
        from omnimarket.nodes.node_demo_renderer_effect.models.model_render_request import (
            ModelDemoRenderRequest,
        )

        handler = NodeDemoRendererEffect()
        req = ModelDemoRenderRequest(
            cost_result=ModelDemoCostResult(
                costs=[
                    ModelDemoCostEntry(
                        model_id="m1",
                        prompt_cost_usd=0.001,
                        completion_cost_usd=0.002,
                        total_cost_usd=0.003,
                        prompt_tokens=100,
                        completion_tokens=50,
                    )
                ],
                cheapest_model_id="m1",
            ),
            bar_width=10,
        )
        result = handler.handle(req)

        captured = capsys.readouterr()
        assert "Model Cost Comparison" in captured.out
        assert "m1" in result.chart_lines[2]
        assert "|##########|" in result.chart_lines[2]
        assert result.chart_lines[-1] == "Cheapest: m1"
