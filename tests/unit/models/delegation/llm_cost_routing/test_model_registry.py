# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for the LLM model registry and pricing manifest (OMN-11779)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.models.delegation.llm_cost_routing import (
    ModelLlmModelProfile,
    ModelLlmModelRegistry,
    ModelLlmModelRegistryLoader,
)

# Path to the bundled registry YAML for integration-style tests
_REGISTRY_PATH = (
    Path(__file__).parent.parent.parent.parent.parent.parent
    / "src"
    / "omnimarket"
    / "data"
    / "model_registry"
    / "model_registry_v1.yaml"
)


class TestModelLlmModelProfile:
    """ModelLlmModelProfile validation."""

    def _valid_kwargs(self) -> dict:
        return {
            "model_id": "qwen3-coder-30b",
            "provider": "local",
            "endpoint_env": "LLM_CODER_URL",
            "context_window": 112000,
            "pricing_per_1m_input": "0.00",
            "pricing_per_1m_output": "0.00",
            "cost_basis": EnumCostBasis.ZERO_MARGINAL_API_COST,
            "observed_at": "2026-05-23T00:00:00Z",
            "source": "manual benchmark",
        }

    def test_instantiation(self) -> None:
        profile = ModelLlmModelProfile(**self._valid_kwargs())
        assert profile.model_id == "qwen3-coder-30b"
        assert profile.context_window == 112000

    def test_pricing_coerced_to_decimal(self) -> None:
        profile = ModelLlmModelProfile(**self._valid_kwargs())
        assert isinstance(profile.pricing_per_1m_input, Decimal)
        assert isinstance(profile.pricing_per_1m_output, Decimal)

    def test_context_window_must_be_positive(self) -> None:
        kwargs = self._valid_kwargs()
        kwargs["context_window"] = 0
        with pytest.raises(ValidationError, match="context_window must be > 0"):
            ModelLlmModelProfile(**kwargs)

    def test_frozen(self) -> None:
        profile = ModelLlmModelProfile(**self._valid_kwargs())
        with pytest.raises((ValidationError, TypeError)):
            profile.model_id = "mutated"  # type: ignore[misc]

    def test_extra_fields_forbidden(self) -> None:
        kwargs = self._valid_kwargs()
        kwargs["surprise"] = "bad"
        with pytest.raises(ValidationError):
            ModelLlmModelProfile(**kwargs)


class TestModelLlmModelRegistryHashing:
    """Registry hash generation must be deterministic."""

    def _make_registry(self) -> ModelLlmModelRegistry:
        profile = ModelLlmModelProfile(
            model_id="test-model",
            provider="local",
            endpoint_env="TEST_URL",
            context_window=8000,
            pricing_per_1m_input="0.00",
            pricing_per_1m_output="0.00",
            cost_basis=EnumCostBasis.ZERO_MARGINAL_API_COST,
            observed_at="2026-05-23T00:00:00Z",
            source="manual benchmark",
        )
        return ModelLlmModelRegistry(
            schema_version="1.0.0",
            model_registry_version="1.0.0",
            pricing_manifest_version="2026-05-23-test",
            observed_at="2026-05-23T00:00:00Z",
            models={"test-model": profile},
        )

    def test_hash_is_computed_on_construction(self) -> None:
        registry = self._make_registry()
        assert registry.model_registry_hash != ""
        assert len(registry.model_registry_hash) == 64  # SHA-256 hex

    def test_pricing_manifest_hash_computed(self) -> None:
        registry = self._make_registry()
        assert registry.pricing_manifest_hash != ""
        assert len(registry.pricing_manifest_hash) == 64

    def test_hash_deterministic_same_content(self) -> None:
        r1 = self._make_registry()
        r2 = self._make_registry()
        assert r1.model_registry_hash == r2.model_registry_hash
        assert r1.pricing_manifest_hash == r2.pricing_manifest_hash

    def test_hash_changes_with_different_pricing(self) -> None:
        profile_cheap = ModelLlmModelProfile(
            model_id="test-model",
            provider="anthropic",
            endpoint_env="TEST_URL",
            context_window=8000,
            pricing_per_1m_input="3.00",
            pricing_per_1m_output="15.00",
            cost_basis=EnumCostBasis.CLOUD_API_COST,
            observed_at="2026-05-23T00:00:00Z",
            source="provider docs",
        )
        profile_expensive = ModelLlmModelProfile(
            model_id="test-model",
            provider="anthropic",
            endpoint_env="TEST_URL",
            context_window=8000,
            pricing_per_1m_input="15.00",
            pricing_per_1m_output="75.00",
            cost_basis=EnumCostBasis.CLOUD_API_COST,
            observed_at="2026-05-23T00:00:00Z",
            source="provider docs",
        )
        base = {
            "schema_version": "1.0.0",
            "model_registry_version": "1.0.0",
            "pricing_manifest_version": "2026-05-23-test",
            "observed_at": "2026-05-23T00:00:00Z",
        }
        r1 = ModelLlmModelRegistry(**base, models={"test-model": profile_cheap})
        r2 = ModelLlmModelRegistry(**base, models={"test-model": profile_expensive})
        assert r1.pricing_manifest_hash != r2.pricing_manifest_hash
        assert r1.model_registry_hash != r2.model_registry_hash


class TestModelLlmModelRegistryGetModel:
    """Registry get_model raises KeyError on missing model."""

    def test_get_model_found(self) -> None:
        profile = ModelLlmModelProfile(
            model_id="m1",
            provider="local",
            endpoint_env="URL",
            context_window=1000,
            pricing_per_1m_input="0.00",
            pricing_per_1m_output="0.00",
            cost_basis=EnumCostBasis.ZERO_MARGINAL_API_COST,
            observed_at="2026-05-23T00:00:00Z",
            source="test",
        )
        registry = ModelLlmModelRegistry(
            schema_version="1.0.0",
            model_registry_version="1.0.0",
            pricing_manifest_version="test",
            observed_at="2026-05-23T00:00:00Z",
            models={"m1": profile},
        )
        assert registry.get_model("m1").model_id == "m1"

    def test_get_model_missing_raises(self) -> None:
        registry = ModelLlmModelRegistry(
            schema_version="1.0.0",
            model_registry_version="1.0.0",
            pricing_manifest_version="test",
            observed_at="2026-05-23T00:00:00Z",
            models={},
        )
        with pytest.raises(KeyError, match="not found in registry"):
            registry.get_model("nonexistent-model")


class TestModelLlmModelRegistryLoader:
    """Loader loads the bundled YAML and validates it."""

    def test_load_bundled_registry(self) -> None:
        loader = ModelLlmModelRegistryLoader(_REGISTRY_PATH)
        registry = loader.load()
        assert registry.schema_version == "1.0.0"
        assert "qwen3-coder-30b" in registry.models
        assert "qwen3.6-35b" in registry.models
        assert "claude-sonnet-4-6" in registry.models
        assert "llama-3.3-70b-free" in registry.models

    def test_current_reasoning_model_is_canonical_qwen36(self) -> None:
        loader = ModelLlmModelRegistryLoader(_REGISTRY_PATH)
        registry = loader.load()
        model = registry.get_model("qwen3.6-35b")

        assert model.endpoint_env == "LLM_QWEN3_NEXT_URL"
        assert model.model_name == "mlx-community/Qwen3.6-35B-A3B-8bit"

    def test_loaded_registry_has_hashes(self) -> None:
        loader = ModelLlmModelRegistryLoader(_REGISTRY_PATH)
        registry = loader.load()
        assert len(registry.model_registry_hash) == 64
        assert len(registry.pricing_manifest_hash) == 64

    def test_loaded_registry_hash_deterministic(self) -> None:
        loader = ModelLlmModelRegistryLoader(_REGISTRY_PATH)
        r1 = loader.load()
        r2 = loader.load()
        assert r1.model_registry_hash == r2.model_registry_hash

    def test_load_nonexistent_raises(self) -> None:
        loader = ModelLlmModelRegistryLoader(Path("/nonexistent/registry.yaml"))
        with pytest.raises(FileNotFoundError, match="Model registry not found"):
            loader.load()

    def test_all_models_have_positive_context_window(self) -> None:
        loader = ModelLlmModelRegistryLoader(_REGISTRY_PATH)
        registry = loader.load()
        for model_id, profile in registry.models.items():
            assert profile.context_window > 0, f"{model_id} has context_window <= 0"

    def test_no_hardcoded_urls_in_registry(self) -> None:
        """All endpoint references must be env var names, not raw URLs."""
        loader = ModelLlmModelRegistryLoader(_REGISTRY_PATH)
        registry = loader.load()
        for model_id, profile in registry.models.items():
            assert not profile.endpoint_env.startswith("http"), (
                f"{model_id}.endpoint_env looks like a URL: {profile.endpoint_env}. "
                "Must be an env var name."
            )

    def test_vertex_gemini_registry_entry_uses_secret_ref(self) -> None:
        """OMN-12971: the Vertex registry entry declares only a logical secret ref.

        The token VALUE is resolved fail-closed at the effect boundary via the
        secret store; the registry carries only the ref NAME. The Vertex path is
        ADDITIVE — the AI Studio ``gemini-2.5-flash-lite`` entry is preserved.
        """
        loader = ModelLlmModelRegistryLoader(_REGISTRY_PATH)
        registry = loader.load()
        assert "vertex-gemini-flash" in registry.models
        vertex = registry.get_model("vertex-gemini-flash")
        assert vertex.provider == "vertex"
        assert vertex.requires_secret_ref == "llm.vertex.access_token"
        assert vertex.requires_api_key_env is None
        assert vertex.endpoint_env == "BIFROST_VERTEX_GEMINI_ENDPOINT_URL"
        # ADDITIVE: AI Studio key path preserved.
        assert "gemini-2.5-flash-lite" in registry.models
