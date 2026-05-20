# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for OMN-7981: contract-driven model routing in node_hostile_reviewer.

Covers:
- build_model_configs loads from contract.yaml model_routing (not hardcoded env)
- N-1 graceful degradation: missing endpoints produce a reduced config, not an error
- build_from_contract returns a fully wired AdapterInferenceBridge
- CLI transport models are included without requiring env vars
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge import (
    AdapterInferenceBridge,
    build_from_contract,
)
from omnimarket.nodes.node_hostile_reviewer.handlers.model_config_loader import (
    build_model_configs,
)


@pytest.mark.unit
class TestBuildModelConfigs:
    """build_model_configs reads contract.yaml, not hardcoded env vars."""

    def test_all_http_missing_returns_empty_http_subset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When HTTP endpoint env vars are unset, HTTP models are skipped."""
        monkeypatch.delenv("LLM_CODER_URL", raising=False)
        monkeypatch.delenv("LLM_DEEPSEEK_R1_URL", raising=False)

        configs = build_model_configs(requested_keys=["coder", "deepseek-r1"])
        assert "coder" not in configs
        assert "deepseek-r1" not in configs

    def test_http_model_included_when_env_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When LLM_CODER_URL is set, coder model is included with correct base_url."""
        monkeypatch.setenv("LLM_CODER_URL", "http://localhost:8000")

        configs = build_model_configs(requested_keys=["coder"])
        assert "coder" in configs
        assert configs["coder"]["base_url"] == "http://localhost:8000"
        assert configs["coder"]["transport"] == "http"

    def test_cli_model_included_without_env(self) -> None:
        """CLI transport models (codex) are included regardless of env vars."""
        configs = build_model_configs(requested_keys=["codex"])
        assert "codex" in configs
        assert configs["codex"]["transport"] == "cli"
        assert configs["codex"]["cli_command"] == "codex"

    def test_n_minus_1_degradation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With one HTTP model down, only the available model is returned."""
        monkeypatch.setenv("LLM_CODER_URL", "http://localhost:8000")
        monkeypatch.delenv("LLM_DEEPSEEK_R1_URL", raising=False)

        configs = build_model_configs(requested_keys=["coder", "deepseek-r1"])
        assert "coder" in configs
        assert "deepseek-r1" not in configs

    def test_requested_keys_filters_contract(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only requested_keys are returned even if other models are configured."""
        monkeypatch.setenv("LLM_CODER_URL", "http://localhost:8000")
        monkeypatch.setenv("LLM_DEEPSEEK_R1_URL", "http://localhost:8001")

        configs = build_model_configs(requested_keys=["coder"])
        assert "coder" in configs
        assert "deepseek-r1" not in configs

    def test_url_trailing_slash_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Trailing slash on base_url is stripped."""
        monkeypatch.setenv("LLM_CODER_URL", "http://localhost:8000/")

        configs = build_model_configs(requested_keys=["coder"])
        assert configs["coder"]["base_url"] == "http://localhost:8000"

    def test_context_window_from_contract(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """context_window declared in contract.yaml is preserved in config."""
        monkeypatch.setenv("LLM_CODER_URL", "http://localhost:8000")

        configs = build_model_configs(requested_keys=["coder"])
        assert configs["coder"]["context_window"] == 112000

    def test_none_requested_keys_loads_all_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """None requested_keys loads every model in model_routing."""
        monkeypatch.setenv("LLM_CODER_URL", "http://localhost:8000")
        monkeypatch.delenv("LLM_DEEPSEEK_R1_URL", raising=False)

        configs = build_model_configs(requested_keys=None)
        # codex (CLI) always available; coder (HTTP) present; deepseek-r1 (HTTP) absent
        assert "codex" in configs
        assert "coder" in configs
        assert "deepseek-r1" not in configs


@pytest.mark.unit
class TestBuildFromContract:
    """build_from_contract returns a wired AdapterInferenceBridge."""

    def test_returns_adapter_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """build_from_contract always returns an AdapterInferenceBridge."""
        monkeypatch.delenv("LLM_CODER_URL", raising=False)
        monkeypatch.delenv("LLM_DEEPSEEK_R1_URL", raising=False)

        adapter = build_from_contract()
        assert isinstance(adapter, AdapterInferenceBridge)

    def test_adapter_has_cli_model_when_all_http_down(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even with all HTTP endpoints down, codex CLI model is available."""
        monkeypatch.delenv("LLM_CODER_URL", raising=False)
        monkeypatch.delenv("LLM_DEEPSEEK_R1_URL", raising=False)

        adapter = build_from_contract()
        assert "codex" in adapter._config.model_configs

    def test_adapter_includes_http_when_env_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When LLM_CODER_URL is set, coder is wired into the adapter."""
        monkeypatch.setenv("LLM_CODER_URL", "http://localhost:8000")

        adapter = build_from_contract(requested_keys=["coder"])
        assert "coder" in adapter._config.model_configs

    def test_unknown_model_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Requesting a model key not in contract raises ValueError on infer()."""
        monkeypatch.delenv("LLM_CODER_URL", raising=False)
        adapter = build_from_contract(requested_keys=["codex"])

        import asyncio

        with pytest.raises(ValueError, match="Unknown model_key"):
            asyncio.run(
                adapter.infer(
                    model_key="nonexistent",
                    system_prompt="s",
                    user_prompt="u",
                    timeout_seconds=5.0,
                )
            )
