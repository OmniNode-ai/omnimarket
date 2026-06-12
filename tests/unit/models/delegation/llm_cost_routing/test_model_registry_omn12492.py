# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for OMN-12492 model registry refresh.

Verifies that the 2026-05-30 registry refresh correctly contains:
- Updated local model facts for Qwen3.6-35B-A3B (.201:8000) and
  Qwen3.6-27B-MTP (.201:8001) under the stable routing keys
- New ds-v4-flash entry (DeepSeek V4 Flash on .200:8101)
- Cloud Gemini Flash entry, retargeted to gemini-2.5-flash-lite (OMN-12937)
- New openrouter-qwen3-coder-480b entry (cheap frontier direct)
- All new entries pass existing registry invariants
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.models.delegation.llm_cost_routing import (
    ModelLlmModelRegistryLoader,
)

_REGISTRY_PATH = (
    Path(__file__).parent.parent.parent.parent.parent.parent
    / "src"
    / "omnimarket"
    / "data"
    / "model_registry"
    / "model_registry_v1.yaml"
)


@pytest.fixture(scope="module")
def registry():  # type: ignore[no-untyped-def]
    loader = ModelLlmModelRegistryLoader(_REGISTRY_PATH)
    return loader.load()


@pytest.mark.unit
class TestOmn12492LocalModelFacts:
    """Local model entries carry correct live-probed facts after OMN-12492 refresh."""

    def test_qwen3_coder_30b_model_name_updated(self, registry) -> None:  # type: ignore[no-untyped-def]
        """qwen3-coder-30b routing key now reflects Qwen3.6-35B-A3B served at .201:8000."""
        model = registry.get_model("qwen3-coder-30b")
        assert model.model_name == "Qwen3.6-35B-A3B"
        assert model.endpoint_env == "LLM_CODER_URL"
        assert model.context_window == 131072

    def test_deepseek_r1_14b_model_name_updated(self, registry) -> None:  # type: ignore[no-untyped-def]
        """deepseek-r1-14b routing key now reflects Qwen3.6-27B-MTP served at .201:8001."""
        model = registry.get_model("deepseek-r1-14b")
        assert model.model_name == "Qwen3.6-27B-MTP-IQ4_XS.gguf"
        assert model.endpoint_env == "LLM_CODER_FAST_URL"
        assert model.context_window == 114688

    def test_ds_v4_flash_present(self, registry) -> None:  # type: ignore[no-untyped-def]
        """ds-v4-flash entry present for DeepSeek V4 Flash on .200:8101."""
        model = registry.get_model("ds-v4-flash")
        assert model.provider == "local"
        assert model.endpoint_env == "LLM_DS_V4_FLASH_URL"
        assert model.model_name == "deepseek-v4-flash"
        assert model.context_window == 65536
        assert model.cost_basis == EnumCostBasis.ZERO_MARGINAL_API_COST

    def test_ds_v4_flash_zero_cost(self, registry) -> None:  # type: ignore[no-untyped-def]
        model = registry.get_model("ds-v4-flash")
        assert model.pricing_per_1m_input == Decimal("0.00")
        assert model.pricing_per_1m_output == Decimal("0.00")


@pytest.mark.unit
class TestOmn12937GeminiFlashLite:
    """gemini-2.5-flash-lite registry entry (OMN-12937).

    OMN-12937: the delegation inference effect sources the Gemini wire model id
    directly from this registry entry (key/model_id/model_name), NOT from the
    bifrost backend. gemini-2.0-flash is free-tier-exhausted (HTTP 429), so the
    registry entry was retargeted to gemini-2.5-flash-lite, which probes HTTP 200
    on the deployed key. See diagnosis agent-live-gemini-proof-OMN-12890.md.
    """

    def test_gemini_flash_lite_present(self, registry) -> None:  # type: ignore[no-untyped-def]
        model = registry.get_model("gemini-2.5-flash-lite")
        assert model.provider == "google"
        assert model.endpoint_env == "GEMINI_API_URL"
        assert model.model_name == "gemini-2.5-flash-lite"
        assert model.requires_api_key_env == "GEMINI_API_KEY"

    def test_gemini_flash_lite_model_id_matches_key(self, registry) -> None:  # type: ignore[no-untyped-def]
        """key == model_id == model_name: the wire id the live effect sends is
        the quota-available model, with no stale alias surface."""
        model = registry.get_model("gemini-2.5-flash-lite")
        assert model.model_id == "gemini-2.5-flash-lite"
        assert model.model_name == "gemini-2.5-flash-lite"

    def test_stale_gemini_2_0_flash_absent(self, registry) -> None:  # type: ignore[no-untyped-def]
        """The free-tier-exhausted gemini-2.0-flash entry must be gone so the
        live effect cannot resolve it and 429."""
        with pytest.raises(KeyError):
            registry.get_model("gemini-2.0-flash")

    def test_gemini_flash_lite_context_window(self, registry) -> None:  # type: ignore[no-untyped-def]
        model = registry.get_model("gemini-2.5-flash-lite")
        assert model.context_window == 1048576  # 1M token context

    def test_gemini_flash_lite_pricing(self, registry) -> None:  # type: ignore[no-untyped-def]
        model = registry.get_model("gemini-2.5-flash-lite")
        assert model.pricing_per_1m_input == Decimal("0.10")
        assert model.pricing_per_1m_output == Decimal("0.40")
        assert model.cost_basis == EnumCostBasis.CLOUD_API_COST

    def test_gemini_endpoint_env_is_not_url(self, registry) -> None:  # type: ignore[no-untyped-def]
        """endpoint_env must be an env var name, not a raw URL (registry invariant)."""
        model = registry.get_model("gemini-2.5-flash-lite")
        assert not model.endpoint_env.startswith("http"), (
            f"endpoint_env must be an env var name, got: {model.endpoint_env}"
        )


@pytest.mark.unit
class TestOmn12492OpenRouterCheapFrontier:
    """openrouter-qwen3-coder-480b entry correct for cheap frontier direct tier."""

    def test_openrouter_qwen3_coder_480b_present(self, registry) -> None:  # type: ignore[no-untyped-def]
        model = registry.get_model("openrouter-qwen3-coder-480b")
        assert model.provider == "openrouter"
        assert model.endpoint_env == "OPENROUTER_URL"
        assert model.model_name == "qwen/qwen3-coder:free"
        assert model.requires_api_key_env == "OPENROUTER_API_KEY"

    def test_openrouter_qwen3_coder_480b_zero_cost(self, registry) -> None:  # type: ignore[no-untyped-def]
        """Free tier model — zero marginal API cost."""
        model = registry.get_model("openrouter-qwen3-coder-480b")
        assert model.pricing_per_1m_input == Decimal("0.00")
        assert model.pricing_per_1m_output == Decimal("0.00")
        assert model.cost_basis == EnumCostBasis.ZERO_MARGINAL_API_COST

    def test_openrouter_qwen3_coder_480b_context_window(self, registry) -> None:  # type: ignore[no-untyped-def]
        model = registry.get_model("openrouter-qwen3-coder-480b")
        assert model.context_window == 262144


@pytest.mark.unit
class TestOmn12492RegistryInvariants:
    """All new models satisfy the existing registry invariants."""

    NEW_MODEL_IDS = [
        "ds-v4-flash",
        "gemini-2.5-flash-lite",
        "openrouter-qwen3-coder-480b",
    ]

    def test_new_models_have_positive_context_window(self, registry) -> None:  # type: ignore[no-untyped-def]
        for model_id in self.NEW_MODEL_IDS:
            model = registry.get_model(model_id)
            assert model.context_window > 0, f"{model_id} has context_window <= 0"

    def test_new_models_endpoint_env_not_url(self, registry) -> None:  # type: ignore[no-untyped-def]
        for model_id in self.NEW_MODEL_IDS:
            model = registry.get_model(model_id)
            assert not model.endpoint_env.startswith("http"), (
                f"{model_id}.endpoint_env looks like a URL: {model.endpoint_env}"
            )

    def test_registry_version_bumped(self, registry) -> None:  # type: ignore[no-untyped-def]
        """OMN-12937 set 1.2.0 (Gemini retarget); OMN-12972 → 1.3.0 (per-env served names)."""
        assert registry.model_registry_version == "1.3.0"

    def test_pricing_manifest_version_updated(self, registry) -> None:  # type: ignore[no-untyped-def]
        assert registry.pricing_manifest_version == "2026-06-11-gemini-25-flash-lite"
