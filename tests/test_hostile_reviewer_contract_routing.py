# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for hostile reviewer caller-supplied logical model routing."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import yaml

from omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge import (
    AdapterInferenceBridge,
    ModelInferenceBridgeConfig,
    build_from_contract,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.model_config_loader import (
    build_model_configs,
)

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_hostile_reviewer"
    / "contract.yaml"
)
ROUTE_CONFIG_ENV = "HOSTILE_REVIEWER_MODEL_CONFIGS_JSON"

RUNTIME_ROUTE_CONFIGS: dict[str, dict[str, object]] = {
    "review_primary": {
        "transport": "http",
        "base_url": "http://localhost:8000/",
        "model_id": "runtime-supplied-model",
        "context_window": 112000,
        "timeout_seconds": 90.0,
    },
    "review_cli": {
        "transport": "cli",
        "cli_command": "codex",
        "context_window": 64000,
        "timeout_seconds": 120.0,
    },
}


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    with CONTRACT_PATH.open() as fh:
        return yaml.safe_load(fh)


@pytest.mark.unit
class TestHostileReviewerContractPolicy:
    """contract.yaml declares routing policy, not served runtime defaults."""

    def test_models_input_requires_caller_keys(self, contract: dict[str, Any]) -> None:
        models_input = contract["inputs"]["models"]
        assert models_input["required"] is True
        assert "default" not in models_input

    def test_contract_has_no_model_specific_endpoint_envs(
        self, contract: dict[str, Any]
    ) -> None:
        rendered = yaml.safe_dump(contract["model_routing"])
        assert "LLM_CODER_URL" not in rendered
        assert "LLM_DEEPSEEK_R1_URL" not in rendered
        assert "endpoint_env" not in rendered

    def test_contract_declares_runtime_route_config_source(
        self, contract: dict[str, Any]
    ) -> None:
        routing = contract["model_routing"]
        assert routing["route_keys_input"] == "models"
        assert routing["route_config_env"] == ROUTE_CONFIG_ENV
        assert routing["route_schema"]["http"]["required_fields"] == [
            "base_url or base_url_env",
            "model_id",
        ]


@pytest.mark.unit
class TestBuildModelConfigs:
    """build_model_configs resolves caller-provided logical route keys."""

    def test_requested_keys_are_required(self) -> None:
        with pytest.raises(ValueError, match="logical model route keys"):
            build_model_configs(requested_keys=None, runtime_model_configs={})

    def test_runtime_route_configs_are_required(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(ROUTE_CONFIG_ENV, raising=False)

        with pytest.raises(ValueError, match=ROUTE_CONFIG_ENV):
            build_model_configs(requested_keys=["review_primary"])

    def test_http_route_included_from_runtime_configs(self) -> None:
        configs = build_model_configs(
            requested_keys=["review_primary"],
            runtime_model_configs=RUNTIME_ROUTE_CONFIGS,
        )

        assert configs["review_primary"]["base_url"] == "http://localhost:8000"
        assert configs["review_primary"]["model_id"] == "runtime-supplied-model"
        assert configs["review_primary"]["transport"] == "http"

    def test_http_route_included_from_env_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(ROUTE_CONFIG_ENV, json.dumps(RUNTIME_ROUTE_CONFIGS))

        configs = build_model_configs(requested_keys=["review_primary"])

        assert configs["review_primary"]["base_url"] == "http://localhost:8000"

    def test_cli_route_requires_runtime_config(self) -> None:
        configs = build_model_configs(
            requested_keys=["review_cli"],
            runtime_model_configs=RUNTIME_ROUTE_CONFIGS,
        )

        assert configs["review_cli"]["transport"] == "cli"
        assert configs["review_cli"]["cli_command"] == "codex"

    def test_missing_requested_route_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="not configured"):
            build_model_configs(
                requested_keys=["missing-route"],
                runtime_model_configs=RUNTIME_ROUTE_CONFIGS,
            )

    def test_http_route_requires_base_url_or_env(self) -> None:
        with pytest.raises(ValueError, match="base_url or base_url_env"):
            build_model_configs(
                requested_keys=["broken"],
                runtime_model_configs={
                    "broken": {"transport": "http", "model_id": "runtime-model"}
                },
            )

    def test_http_route_requires_model_id(self) -> None:
        with pytest.raises(ValueError, match="requires runtime model_id"):
            build_model_configs(
                requested_keys=["broken"],
                runtime_model_configs={
                    "broken": {
                        "transport": "http",
                        "base_url": "http://localhost:8000",
                    }
                },
            )

    def test_context_window_from_runtime_config(self) -> None:
        configs = build_model_configs(
            requested_keys=["review_primary"],
            runtime_model_configs=RUNTIME_ROUTE_CONFIGS,
        )

        assert configs["review_primary"]["context_window"] == 112000


@pytest.mark.unit
class TestBuildFromContract:
    """build_from_contract returns a wired AdapterInferenceBridge."""

    def test_requires_requested_keys(self) -> None:
        with pytest.raises(ValueError, match="logical model route keys"):
            build_from_contract(runtime_model_configs=RUNTIME_ROUTE_CONFIGS)

    def test_returns_adapter_instance(self) -> None:
        adapter = build_from_contract(
            requested_keys=["review_primary"],
            runtime_model_configs=RUNTIME_ROUTE_CONFIGS,
        )

        assert isinstance(adapter, AdapterInferenceBridge)
        assert "review_primary" in adapter._config.model_configs

    def test_unknown_model_key_raises(self) -> None:
        adapter = build_from_contract(
            requested_keys=["review_cli"],
            runtime_model_configs=RUNTIME_ROUTE_CONFIGS,
        )

        with pytest.raises(ValueError, match="Unknown model_key"):
            asyncio.run(
                adapter.infer(
                    model_key="nonexistent",
                    system_prompt="s",
                    user_prompt="u",
                    timeout_seconds=5.0,
                )
            )

    def test_adapter_does_not_fallback_to_model_key_as_model_id(self) -> None:
        adapter = AdapterInferenceBridge(
            ModelInferenceBridgeConfig(
                model_configs={
                    "review_primary": {
                        "transport": "http",
                        "base_url": "http://localhost:8000",
                    }
                }
            )
        )

        with pytest.raises(ValueError, match="missing model_id"):
            asyncio.run(
                adapter.infer(
                    model_key="review_primary",
                    system_prompt="s",
                    user_prompt="u",
                    timeout_seconds=5.0,
                )
            )

    def test_adapter_uses_runtime_model_id(self) -> None:
        adapter = build_from_contract(
            requested_keys=["review_primary"],
            runtime_model_configs=RUNTIME_ROUTE_CONFIGS,
        )

        async def run() -> None:
            with patch.object(
                adapter, "_call_http_model", new_callable=AsyncMock
            ) as mock_call:
                mock_call.return_value = "ok"
                result = await adapter.infer(
                    model_key="review_primary",
                    system_prompt="s",
                    user_prompt="u",
                    timeout_seconds=5.0,
                )
                assert result == "ok"
                cfg = mock_call.call_args.args[1]
                assert cfg["model_id"] == "runtime-supplied-model"

        asyncio.run(run())
