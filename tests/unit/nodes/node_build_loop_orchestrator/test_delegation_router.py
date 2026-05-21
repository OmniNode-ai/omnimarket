# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the delegation router — ticket-to-model-tier routing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.nodes.node_build_loop_orchestrator.handlers import (
    adapter_delegation_router,
)
from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_delegation_router import (
    EnumModelTier,
    ModelDelegationHarnessSample,
    build_endpoint_configs,
    route_ticket_to_tier,
)

# Tier set without GLM — tests local-only routing fallback behavior
_LOCAL_ONLY = frozenset(
    {EnumModelTier.LOCAL_FAST, EnumModelTier.LOCAL_CODER, EnumModelTier.LOCAL_REASONING}
)

_LOCAL_PLUS_GOOGLE = _LOCAL_ONLY | {EnumModelTier.FRONTIER_GOOGLE}
_LOCAL_PLUS_GEMINI_CLI = _LOCAL_ONLY | {EnumModelTier.GEMINI_CLI}
_LOCAL_PLUS_GEMINI_CLI_AND_GOOGLE = _LOCAL_PLUS_GEMINI_CLI | {
    EnumModelTier.FRONTIER_GOOGLE
}


@pytest.mark.unit
class TestRouteTicketToTier:
    """Test ticket routing to model tiers."""

    def test_glm_is_primary_when_available(self) -> None:
        """GLM-4.5 should be selected for any task when available."""
        tier = route_ticket_to_tier(
            "add unit tests for handler",
            "write comprehensive tests",
        )
        assert tier == EnumModelTier.FRONTIER_GLM

    def test_simple_task_routes_to_local_fast_without_glm(self) -> None:
        tier = route_ticket_to_tier(
            "fix lint error", "rename import", available_tiers=_LOCAL_ONLY
        )
        assert tier == EnumModelTier.LOCAL_FAST

    def test_complex_task_routes_to_frontier_without_glm(self) -> None:
        tier = route_ticket_to_tier(
            "design new event bus architecture",
            "multi-repo migration needed",
            available_tiers=_LOCAL_PLUS_GOOGLE,
        )
        assert tier == EnumModelTier.FRONTIER_GOOGLE

    def test_medium_task_routes_to_local_coder_without_glm(self) -> None:
        tier = route_ticket_to_tier(
            "add unit tests for handler",
            "write comprehensive tests",
            available_tiers=_LOCAL_ONLY,
        )
        assert tier == EnumModelTier.LOCAL_CODER

    def test_format_task_routes_to_local_fast_without_glm(self) -> None:
        tier = route_ticket_to_tier(
            "format code", "run ruff format", available_tiers=_LOCAL_ONLY
        )
        assert tier == EnumModelTier.LOCAL_FAST

    def test_pipeline_task_routes_to_frontier_without_glm(self) -> None:
        tier = route_ticket_to_tier(
            "wire kafka pipeline",
            "new pipeline for event processing",
            available_tiers=_LOCAL_PLUS_GOOGLE,
        )
        assert tier == EnumModelTier.FRONTIER_GOOGLE

    def test_fallback_when_frontier_unavailable(self) -> None:
        tier = route_ticket_to_tier(
            "design new architecture",
            "complex multi-repo change",
            available_tiers=frozenset(
                {EnumModelTier.LOCAL_FAST, EnumModelTier.LOCAL_CODER}
            ),
        )
        assert tier == EnumModelTier.LOCAL_CODER

    def test_unknown_task_defaults_to_local_coder_without_glm(self) -> None:
        tier = route_ticket_to_tier(
            "some generic ticket",
            "do something interesting",
            available_tiers=_LOCAL_ONLY,
        )
        assert tier == EnumModelTier.LOCAL_CODER

    def test_mature_harness_samples_override_primary_routing(self) -> None:
        """Once >=20 samples exist, router should use harness quality evidence."""
        samples = [
            ModelDelegationHarnessSample(
                model_key=EnumModelTier.FRONTIER_GLM.value,
                task_type="code_generation",
                score=0.30,
            )
            for _ in range(20)
        ] + [
            ModelDelegationHarnessSample(
                model_key=EnumModelTier.LOCAL_CODER.value,
                task_type="code_generation",
                score=0.91,
            )
            for _ in range(20)
        ]

        tier = route_ticket_to_tier(
            "add unit tests for handler",
            "write comprehensive tests",
            harness_samples=samples,
        )

        assert tier == EnumModelTier.LOCAL_CODER

    def test_immature_harness_samples_do_not_override_primary_routing(self) -> None:
        samples = [
            ModelDelegationHarnessSample(
                model_key=EnumModelTier.LOCAL_CODER.value,
                task_type="code_generation",
                score=0.99,
            )
            for _ in range(19)
        ]

        tier = route_ticket_to_tier(
            "add unit tests for handler",
            "write comprehensive tests",
            harness_samples=samples,
        )

        assert tier == EnumModelTier.FRONTIER_GLM

    def test_architecture_task_routes_to_gemini_cli_when_available(self) -> None:
        """Gemini CLI should be preferred over FRONTIER_GOOGLE for architecture tasks."""
        tier = route_ticket_to_tier(
            "design new architecture for event bus",
            "multi-file refactor needed across repos",
            available_tiers=_LOCAL_PLUS_GEMINI_CLI_AND_GOOGLE,
        )
        assert tier == EnumModelTier.GEMINI_CLI

    def test_multi_file_task_routes_to_gemini_cli_when_available(self) -> None:
        tier = route_ticket_to_tier(
            "cross-repo migration of schema",
            "breaking change across multiple services",
            available_tiers=_LOCAL_PLUS_GEMINI_CLI,
        )
        assert tier == EnumModelTier.GEMINI_CLI

    def test_gemini_cli_preferred_over_frontier_google_for_complex_tasks(self) -> None:
        tier = route_ticket_to_tier(
            "new service orchestrator pipeline",
            "kafka event bus migration",
            available_tiers=_LOCAL_PLUS_GEMINI_CLI_AND_GOOGLE,
        )
        assert tier == EnumModelTier.GEMINI_CLI

    def test_gemini_cli_fallback_to_frontier_google_when_cli_unavailable(self) -> None:
        """When GEMINI_CLI is unavailable, complex tasks fall back to FRONTIER_GOOGLE."""
        tier = route_ticket_to_tier(
            "design new architecture",
            "multi-repo migration needed",
            available_tiers=_LOCAL_PLUS_GOOGLE,
        )
        assert tier == EnumModelTier.FRONTIER_GOOGLE

    def test_gemini_cli_fallback_to_local_coder_when_no_frontier(self) -> None:
        """When no frontier tier is available, complex tasks fall back to LOCAL_CODER."""
        tier = route_ticket_to_tier(
            "design new architecture",
            "new node orchestrator pipeline",
            available_tiers=frozenset(
                {EnumModelTier.LOCAL_FAST, EnumModelTier.LOCAL_CODER}
            ),
        )
        assert tier == EnumModelTier.LOCAL_CODER

    def test_gemini_cli_not_selected_for_simple_tasks(self) -> None:
        """Simple tasks should not be routed to GEMINI_CLI."""
        tier = route_ticket_to_tier(
            "fix lint error",
            "rename import",
            available_tiers=_LOCAL_PLUS_GEMINI_CLI,
        )
        assert tier == EnumModelTier.LOCAL_FAST

    def test_reads_harness_result_json_for_mature_samples(self, tmp_path: Path) -> None:
        harness_result = tmp_path / "llm_eval_results.json"
        samples = [
            {
                "model_key": EnumModelTier.FRONTIER_GLM.value,
                "task_id": f"glm-{index}",
                "task_type": "code_generation",
                "score": 0.30,
            }
            for index in range(20)
        ] + [
            {
                "model_key": EnumModelTier.LOCAL_CODER.value,
                "task_id": f"coder-{index}",
                "task_type": "code_generation",
                "score": 0.91,
            }
            for index in range(20)
        ]
        harness_result.write_text(json.dumps({"samples": samples}))

        tier = route_ticket_to_tier(
            "add unit tests for handler",
            "write comprehensive tests",
            harness_result_path=harness_result,
        )

        assert tier == EnumModelTier.LOCAL_CODER


_ENDPOINT_ENV_KEYS = (
    "LLM_GLM_API_KEY",
    "LLM_GLM_URL",
    "LLM_GLM_MODEL_NAME",
    "LLM_CODER_FAST_URL",
    "LLM_CODER_URL",
    "LLM_DEEPSEEK_R1_URL",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENAI_API_KEY",
)


def _clear_endpoint_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENDPOINT_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


@pytest.mark.unit
class TestBuildEndpointConfigs:
    """Endpoint config proof without depending on host environment variables."""

    def test_empty_env_builds_no_endpoint_configs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_endpoint_env(monkeypatch)

        configs = build_endpoint_configs()

        assert configs == {}

    def test_glm_endpoint_requires_key_and_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_endpoint_env(monkeypatch)
        monkeypatch.setenv("LLM_GLM_API_KEY", "secret")

        assert EnumModelTier.FRONTIER_GLM not in build_endpoint_configs()

        monkeypatch.setenv("LLM_GLM_URL", "https://glm.example/v4")
        monkeypatch.setenv("LLM_GLM_MODEL_NAME", "glm-4.5")

        configs = build_endpoint_configs()

        assert configs[EnumModelTier.FRONTIER_GLM].base_url == "https://glm.example/v4"
        assert configs[EnumModelTier.FRONTIER_GLM].model_id == "glm-4.5"
        assert configs[EnumModelTier.FRONTIER_REVIEW].model_id == "glm-4.7-flash"

    def test_gemini_key_uses_cli_when_binary_is_available(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_endpoint_env(monkeypatch)
        monkeypatch.setenv("GEMINI_API_KEY", "secret")
        monkeypatch.setattr(
            adapter_delegation_router, "_gemini_cli_available", lambda: True
        )

        configs = build_endpoint_configs()

        assert configs[EnumModelTier.GEMINI_CLI].base_url == "cli://gemini"
        assert configs[EnumModelTier.FRONTIER_GOOGLE].model_id == "gemini-2.5-flash"

    def test_gemini_key_skips_cli_when_binary_is_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_endpoint_env(monkeypatch)
        monkeypatch.setenv("GOOGLE_API_KEY", "secret")
        monkeypatch.setattr(
            adapter_delegation_router, "_gemini_cli_available", lambda: False
        )

        configs = build_endpoint_configs()

        assert EnumModelTier.GEMINI_CLI not in configs
        assert configs[EnumModelTier.FRONTIER_GOOGLE].api_key == "secret"
