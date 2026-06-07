# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-12802 — endpoint resolution from the bifrost routing authority (no env).

Each backend's endpoint_url is the FULL provider-correct URL. The resolver must
return it verbatim for any provider (local vLLM, cloud Gemini, ...) — proving the
"one shared base + path append" problem is gone — and must FAIL CLOSED when a
backend is missing or has no configured endpoint. No shared LLM_*_URL env var is
read anywhere on this path.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.routing.endpoint_resolver import (
    ModelResolvedEndpoint,
    resolve_endpoint,
)

# The canonical bifrost config (backends declared, endpoints null — filled by
# overlay at runtime). The resolver reads this same authority the delegation
# router uses; the test supplies an overlay that fills two backends' endpoints.
_REAL_CONFIG = (
    Path(__file__).parent.parent.parent.parent
    / "src"
    / "omnimarket"
    / "configs"
    / "bifrost_delegation.yaml"
)

# Two providers with DIFFERENT URLs and path conventions — the whole point of
# per-backend resolution. A single base + append cannot produce both.
_VLLM_URL = "http://192.168.86.201:8000/v1/chat/completions"  # onex-allow-internal-ip OMN-12802 reason="test fixture: representative local vLLM endpoint proving resolution returns the full URL"
_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"


def _write_overlay(tmp_path: Path) -> Path:
    """Overlay that fills endpoint_url/model_name for local-coder + cloud-gemini-flash.

    Mirrors how ~/.omninode/delegation/bifrost_overrides.yaml supplies endpoints
    at runtime, deep-merged by backend_id over the repo default.
    """
    overlay = {
        "backends": [
            {
                "backend_id": "local-coder",
                "endpoint_url": _VLLM_URL,
                "model_name": "Qwen3.6-35B-A3B",
            },
            {
                "backend_id": "cloud-gemini-flash",
                "endpoint_url": _GEMINI_URL,
                "model_name": "gemini-2.0-flash",
            },
        ]
    }
    path = tmp_path / "bifrost_overrides.yaml"
    path.write_text(yaml.safe_dump(overlay), encoding="utf-8")
    return path


@pytest.mark.unit
class TestResolveEndpointProviderAgnostic:
    def test_local_vllm_resolves_full_chat_completions_url(
        self, tmp_path: Path
    ) -> None:
        overlay = _write_overlay(tmp_path)
        resolved = resolve_endpoint(
            "local-coder", config_path=_REAL_CONFIG, overlay_path=overlay
        )
        assert isinstance(resolved, ModelResolvedEndpoint)
        # The full provider-correct URL, NOT a bare base — proves the 404 root-POST
        # defect is fixed without any path-append mode logic.
        assert resolved.endpoint_url == _VLLM_URL
        assert resolved.endpoint_url.endswith("/v1/chat/completions")
        assert resolved.model_name == "Qwen3.6-35B-A3B"
        assert resolved.api_key_ref is None

    def test_cloud_gemini_resolves_its_own_distinct_url(self, tmp_path: Path) -> None:
        overlay = _write_overlay(tmp_path)
        resolved = resolve_endpoint(
            "cloud-gemini-flash", config_path=_REAL_CONFIG, overlay_path=overlay
        )
        # A DIFFERENT URL than vLLM — a single base + append could never produce
        # both. This is the provider-agnostic proof.
        assert resolved.endpoint_url == _GEMINI_URL
        assert resolved.endpoint_url != _VLLM_URL
        assert resolved.model_name == "gemini-2.0-flash"
        assert resolved.api_key_ref == "GEMINI_API_KEY"


@pytest.mark.unit
class TestResolveEndpointFailClosed:
    def test_unknown_backend_raises(self, tmp_path: Path) -> None:
        overlay = _write_overlay(tmp_path)
        with pytest.raises(ValueError, match="not declared"):
            resolve_endpoint(
                "nonexistent-backend",
                config_path=_REAL_CONFIG,
                overlay_path=overlay,
            )

    def test_backend_without_endpoint_raises_no_silent_default(
        self, tmp_path: Path
    ) -> None:
        # local-reasoner is a real backend with no overlay endpoint — fail closed.
        overlay = _write_overlay(tmp_path)
        with pytest.raises(ValueError, match="no configured endpoint_url"):
            resolve_endpoint(
                "local-reasoner",
                config_path=_REAL_CONFIG,
                overlay_path=overlay,
            )

    def test_empty_backend_ref_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="non-empty backend_ref"):
            resolve_endpoint("")
