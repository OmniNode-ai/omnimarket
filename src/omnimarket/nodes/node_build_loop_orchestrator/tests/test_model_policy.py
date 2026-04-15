# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for model_policy.yaml resolution — OMN-8782.

TDD-first: these tests must fail before the policy loader exists, then pass after.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Locate the model_policy.yaml file relative to the package root
# ---------------------------------------------------------------------------
_OMNIMARKET_SRC = Path(__file__).parents[5]  # src/omnimarket/../../../..
_MODEL_POLICY_PATH = Path(__file__).parents[5] / "omnimarket" / "model_policy.yaml"


def _find_model_policy() -> Path:
    """Locate model_policy.yaml by walking up from this file."""
    # src/omnimarket/nodes/node_build_loop_orchestrator/tests/test_model_policy.py
    # -> src/omnimarket/model_policy.yaml
    candidate = Path(__file__).parents[3] / "model_policy.yaml"
    if candidate.exists():
        return candidate
    # fallback: package root
    for parent in Path(__file__).parents:
        p = parent / "model_policy.yaml"
        if p.exists():
            return p
    raise FileNotFoundError(f"model_policy.yaml not found relative to {__file__}")


class TestModelPolicyFileExists:
    def test_model_policy_yaml_exists(self) -> None:
        """model_policy.yaml must exist at src/omnimarket/model_policy.yaml."""
        path = _find_model_policy()
        assert path.exists(), f"model_policy.yaml not found at {path}"

    def test_model_policy_has_required_sections(self) -> None:
        """model_policy.yaml must declare all 6 required LLM tier policies."""
        import yaml

        path = _find_model_policy()
        data = yaml.safe_load(path.read_text())
        assert "policies" in data, "model_policy.yaml must have a 'policies' key"
        policies = data["policies"]
        required = {
            "coder",
            "coder_fast",
            "judge",
            "delegation",
            "delegation_review",
            "embedding",
        }
        missing = required - set(policies)
        assert not missing, f"model_policy.yaml is missing required policies: {missing}"

    def test_coder_policy_has_tier_and_fallback(self) -> None:
        import yaml

        path = _find_model_policy()
        data = yaml.safe_load(path.read_text())
        coder = data["policies"]["coder"]
        assert "tier" in coder, "policies.coder must have 'tier'"
        assert "fallback_tier" in coder, "policies.coder must have 'fallback_tier'"

    def test_judge_policy_has_env_var(self) -> None:
        import yaml

        path = _find_model_policy()
        data = yaml.safe_load(path.read_text())
        judge = data["policies"]["judge"]
        assert "env_var" in judge, (
            "policies.judge must declare the env_var for URL resolution"
        )


class TestModelPolicyLoader:
    """Tests for the ModelPolicyLoader that resolves policy IDs to URLs."""

    def test_loader_resolves_coder_url_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Loader must resolve coder URL from env var, not hardcoded IP."""
        monkeypatch.setenv("LLM_CODER_URL", "http://test-host:8000")
        from omnimarket.nodes.node_build_loop_orchestrator.handlers.model_policy_loader import (
            ModelPolicyLoader,
        )

        loader = ModelPolicyLoader()
        url = loader.resolve("coder")
        assert url == "http://test-host:8000"

    def test_loader_resolves_coder_fast_url_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_CODER_FAST_URL", "http://test-host:8001")
        from omnimarket.nodes.node_build_loop_orchestrator.handlers.model_policy_loader import (
            ModelPolicyLoader,
        )

        loader = ModelPolicyLoader()
        url = loader.resolve("coder_fast")
        assert url == "http://test-host:8001"

    def test_loader_resolves_judge_url_from_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_DEEPSEEK_R1_URL", "http://test-host:8101")
        from omnimarket.nodes.node_build_loop_orchestrator.handlers.model_policy_loader import (
            ModelPolicyLoader,
        )

        loader = ModelPolicyLoader()
        url = loader.resolve("judge")
        assert url == "http://test-host:8101"

    def test_loader_raises_on_missing_env_var(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Loader must raise RuntimeError when env var is absent — no silent fallback."""
        monkeypatch.delenv("LLM_CODER_URL", raising=False)
        monkeypatch.delenv("LLM_CODER_FAST_URL", raising=False)
        monkeypatch.delenv("LLM_DEEPSEEK_R1_URL", raising=False)
        from omnimarket.nodes.node_build_loop_orchestrator.handlers.model_policy_loader import (
            ModelPolicyLoader,
        )

        loader = ModelPolicyLoader()
        with pytest.raises(RuntimeError, match="not configured"):
            loader.resolve("coder")

    def test_no_hardcoded_ips_in_loader(self) -> None:
        """The policy loader must never contain hardcoded 192.168.x.x IPs."""
        # parents[1] = node_build_loop_orchestrator
        loader_path = Path(__file__).parents[1] / "handlers" / "model_policy_loader.py"
        if not loader_path.exists():
            pytest.skip(
                "model_policy_loader.py not yet created (TDD: expected to fail)"
            )
        content = loader_path.read_text()
        ip_pattern = re.compile(r"192\.168\.\d+\.\d+")
        assert not ip_pattern.search(content), (
            f"Hardcoded IP found in {loader_path}: {ip_pattern.findall(content)}"
        )


class TestNoHardcodedIpsInHandlers:
    """CI-style assertions: no hardcoded 192.168.x.x IPs in handler source."""

    # parents[1] = node_build_loop_orchestrator
    # parents[2] = nodes
    _HANDLER_FILES = [
        Path(__file__).parents[1] / "handlers" / "adapter_delegation_router.py",
        Path(__file__).parents[1] / "handlers" / "adapter_llm_classify.py",
        Path(__file__).parents[1] / "assemble_live.py",
        Path(__file__).parents[2]
        / "node_pr_review_bot"
        / "handlers"
        / "handler_judge_verifier.py",
    ]

    @pytest.mark.parametrize("handler_path", _HANDLER_FILES)
    def test_no_hardcoded_ips(self, handler_path: Path) -> None:
        if not handler_path.exists():
            pytest.skip(f"{handler_path.name} not found")
        content = handler_path.read_text()
        ip_pattern = re.compile(r"192\.168\.\d+\.\d+")
        matches = ip_pattern.findall(content)
        assert not matches, (
            f"Hardcoded IP(s) {matches} found in {handler_path.name}. "
            "Use env vars or model_policy.yaml instead."
        )

    def test_no_hardcoded_volumes_path_in_assemble_live(self) -> None:
        """assemble_live.py must not contain hardcoded /Volumes/ paths."""
        path = Path(__file__).parents[1] / "assemble_live.py"
        if not path.exists():
            pytest.skip("assemble_live.py not found")
        content = path.read_text()
        assert "/Volumes/" not in content, (
            "Hardcoded /Volumes/ path found in assemble_live.py — use Path.home() or env var"
        )
