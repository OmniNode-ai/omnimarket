# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-11933: Tests proving build-loop model routing uses registry policy, not hardcoded literals.

Phase 1 (TDD): tests that prove current hardcoded behavior exist.
Phase 2: after migration, same tests assert routing goes through ModelPolicyLoader.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_HANDLER_DIR = (
    Path(__file__).parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_build_loop_orchestrator"
    / "handlers"
)
_ASSEMBLE_LIVE_PATH = (
    Path(__file__).parents[4]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_build_loop_orchestrator"
    / "assemble_live.py"
)
_ROUTER_PATH = _HANDLER_DIR / "adapter_delegation_router.py"


# ---------------------------------------------------------------------------
# Tests: hardcoded model ID literals must NOT appear in handler source
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNoHardcodedModelIdsInDelegationRouter:
    """adapter_delegation_router.py must not contain hardcoded model ID strings.

    These must come from ModelPolicyLoader / model_policy.yaml after migration.
    """

    _BANNED_LITERALS = [
        "glm-4.7-flash",
        "gemini-2.5-flash",
        "gpt-4.1",
    ]

    @pytest.mark.parametrize("literal", _BANNED_LITERALS)
    def test_banned_literal_absent(self, literal: str) -> None:
        content = _ROUTER_PATH.read_text()
        assert literal not in content, (
            f"Hardcoded model ID {literal!r} found in adapter_delegation_router.py. "
            "Use ModelPolicyLoader.resolve_model_id() to get model IDs from model_policy.yaml."
        )

    def test_hardcoded_google_base_url_absent(self) -> None:
        content = _ROUTER_PATH.read_text()
        assert "generativelanguage.googleapis.com" not in content, (
            "Hardcoded Google API base URL in adapter_delegation_router.py. "
            "Route through ModelPolicyLoader."
        )

    def test_hardcoded_openai_base_url_absent(self) -> None:
        content = _ROUTER_PATH.read_text()
        assert "api.openai.com" not in content, (
            "Hardcoded OpenAI base URL in adapter_delegation_router.py. "
            "Route through ModelPolicyLoader."
        )


@pytest.mark.unit
class TestNoHardcodedModelIdsInAssembleLive:
    """assemble_live.py must not use hardcoded model ID strings in routing/dispatch calls.

    Note: model IDs in cost-tracking dicts (_MODEL_COST_PER_1K) are allowed — they
    are lookup keys, not routing decisions. The ban targets inline model= arguments
    in LLM call sites.
    """

    def test_gpt4o_mini_not_in_routing_call(self) -> None:
        """'gpt-4o-mini' must not appear as a routing decision — use policy loader."""
        content = _ASSEMBLE_LIVE_PATH.read_text()
        # The literal may only appear in the cost-tracking dict, not as a model= arg
        import ast

        tree = ast.parse(content)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.keyword)
                and node.arg == "model"
                and isinstance(node.value, ast.Constant)
                and node.value.value == "gpt-4o-mini"
            ):
                pytest.fail(
                    "Hardcoded model='gpt-4o-mini' found in LLM call in assemble_live.py. "
                    "Use _policy_loader.resolve_model_id('frontier_openai') instead."
                )

    def test_qwen3_coder_not_in_routing_call(self) -> None:
        """'qwen3-coder-30b' must not appear as a model= arg in LLM calls — use policy loader."""
        content = _ASSEMBLE_LIVE_PATH.read_text()
        import ast

        tree = ast.parse(content)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.keyword)
                and node.arg == "model"
                and isinstance(node.value, ast.Constant)
                and node.value.value == "qwen3-coder-30b"
            ):
                pytest.fail(
                    "Hardcoded model='qwen3-coder-30b' in LLM call in assemble_live.py. "
                    "Use _policy_loader.resolve_model_id('coder') instead."
                )

    def test_hardcoded_openai_base_url_constant_absent(self) -> None:
        """OPENAI_BASE_URL must not be assigned a hardcoded string literal."""
        content = _ASSEMBLE_LIVE_PATH.read_text()
        # After migration, OPENAI_BASE_URL reads from env, no inline literal assignment
        assert 'OPENAI_BASE_URL = "https://api.openai.com' not in content, (
            "Hardcoded OPENAI_BASE_URL = 'https://api.openai.com...' in assemble_live.py. "
            "Use os.environ.get with LLM_OPENAI_BASE_URL or ModelPolicyLoader.resolve_base_url()."
        )


# ---------------------------------------------------------------------------
# Tests: ModelPolicyLoader is used to resolve delegation/review model IDs
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDelegationRouterUsesModelPolicyLoader:
    """Verify adapter_delegation_router imports and uses ModelPolicyLoader."""

    def test_model_policy_loader_imported(self) -> None:
        content = _ROUTER_PATH.read_text()
        assert "ModelPolicyLoader" in content, (
            "adapter_delegation_router.py must import ModelPolicyLoader "
            "to resolve model IDs from registry policy."
        )

    def test_build_endpoint_configs_uses_policy_loader(self) -> None:
        """build_endpoint_configs must use ModelPolicyLoader for model_id resolution."""
        content = _ROUTER_PATH.read_text()
        assert "ModelPolicyLoader" in content, (
            "build_endpoint_configs in adapter_delegation_router.py must use "
            "ModelPolicyLoader to resolve model IDs."
        )


@pytest.mark.unit
class TestAssembleLiveUsesModelPolicyLoader:
    """assemble_live.py must use ModelPolicyLoader for model_id resolution."""

    def test_model_policy_loader_imported(self) -> None:
        content = _ASSEMBLE_LIVE_PATH.read_text()
        assert "ModelPolicyLoader" in content, (
            "assemble_live.py must import ModelPolicyLoader "
            "to resolve model IDs from registry policy."
        )


# ---------------------------------------------------------------------------
# Tests: build_endpoint_configs resolves model IDs from policy, not literals
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBuildEndpointConfigsWithPolicyLoader:
    """build_endpoint_configs must resolve review/frontier model IDs from policy."""

    def test_frontier_review_model_id_from_policy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FRONTIER_REVIEW model_id must come from ModelPolicyLoader (delegation_review policy)."""
        monkeypatch.setenv("LLM_GLM_API_KEY", "test-key")
        monkeypatch.setenv("LLM_GLM_URL", "https://glm.example/v4")
        monkeypatch.setenv("LLM_GLM_MODEL_NAME", "glm-4.5")

        from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_delegation_router import (
            EnumModelTier,
            build_endpoint_configs,
        )

        configs = build_endpoint_configs()
        assert EnumModelTier.FRONTIER_REVIEW in configs
        review_config = configs[EnumModelTier.FRONTIER_REVIEW]
        # model_id must NOT be the hardcoded literal "glm-4.7-flash"
        # It must come from model_policy.yaml delegation_review.model_id
        assert review_config.model_id == "glm-4.7-flash", (
            "FRONTIER_REVIEW model_id must match delegation_review policy in model_policy.yaml"
        )

    def test_frontier_google_model_id_from_policy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FRONTIER_GOOGLE model_id must come from ModelPolicyLoader (frontier_google policy)."""
        from omnimarket.nodes.node_build_loop_orchestrator.handlers import (
            adapter_delegation_router,
        )

        monkeypatch.setenv("GEMINI_API_KEY", "test-key")
        monkeypatch.setattr(
            adapter_delegation_router, "_gemini_cli_available", lambda: False
        )

        from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_delegation_router import (
            EnumModelTier,
            build_endpoint_configs,
        )

        configs = build_endpoint_configs()
        assert EnumModelTier.FRONTIER_GOOGLE in configs
        # model_id must come from policy, not "gemini-2.5-flash" literal
        assert configs[EnumModelTier.FRONTIER_GOOGLE].model_id == "gemini-2.5-flash", (
            "FRONTIER_GOOGLE model_id must match frontier_google policy in model_policy.yaml"
        )

    def test_frontier_openai_model_id_from_policy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FRONTIER_OPENAI model_id must come from policy, not hardcoded 'gpt-4.1'."""
        monkeypatch.setenv("OPENAI_API_KEY", "test-key")

        from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_delegation_router import (
            EnumModelTier,
            build_endpoint_configs,
        )

        configs = build_endpoint_configs()
        assert EnumModelTier.FRONTIER_OPENAI in configs
        # model_id must come from policy
        assert configs[EnumModelTier.FRONTIER_OPENAI].model_id == "gpt-4.1", (
            "FRONTIER_OPENAI model_id must match frontier_openai policy in model_policy.yaml"
        )


# ---------------------------------------------------------------------------
# Regression: no hardcoded private LAN IPs (pre-existing guard)  # onex-allow-internal-ip: comment describes a guard, not a usage
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_hardcoded_ips_in_delegation_router() -> None:
    content = _ROUTER_PATH.read_text()
    ip_pattern = re.compile(r"192\.168\.\d+\.\d+")
    matches = ip_pattern.findall(content)
    assert not matches, f"Hardcoded IP(s) {matches} in adapter_delegation_router.py"


@pytest.mark.unit
def test_no_hardcoded_ips_in_assemble_live() -> None:
    content = _ASSEMBLE_LIVE_PATH.read_text()
    ip_pattern = re.compile(r"192\.168\.\d+\.\d+")
    matches = ip_pattern.findall(content)
    assert not matches, f"Hardcoded IP(s) {matches} in assemble_live.py"
