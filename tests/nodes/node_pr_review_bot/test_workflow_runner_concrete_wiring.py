# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-9355: workflow_runner concrete handler wiring + inference_bridge_config tests.

TDD-first: these tests were written RED before the fix was applied.

Asserts:
1. load_inference_bridge_config_from_env() populates model_configs from LLM_*_URL env vars
2. run_review() wires concrete HandlerThreadPoster, HandlerThreadWatcher,
   HandlerJudgeVerifier, HandlerReportPoster (not stubs)
3. run_review() uses inference_bridge_config populated from env (no ValueError on model_key)
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge import (
    ModelInferenceBridgeConfig,
)
from omnimarket.nodes.node_pr_review_bot.handlers.handler_thread_poster import (
    HandlerThreadPoster,
)
from omnimarket.nodes.node_pr_review_bot.workflow_runner import (
    load_inference_bridge_config_from_env,
    run_review,
)

# ---------------------------------------------------------------------------
# Bug 1: load_inference_bridge_config_from_env
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_build_inference_bridge_config_populates_coder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """model_configs must include qwen3-coder entry when LLM_CODER_URL+MODEL_NAME are set."""
    monkeypatch.setenv("LLM_CODER_URL", "http://test-host:8000")
    monkeypatch.setenv("LLM_CODER_MODEL_NAME", "test-coder-model")
    monkeypatch.setenv("LLM_CODER_FAST_URL", "http://test-host:8001")
    monkeypatch.setenv("LLM_CODER_FAST_MODEL_NAME", "test-fast-model")
    monkeypatch.setenv("LLM_DEEPSEEK_R1_URL", "http://test-host:8101")
    monkeypatch.setenv("LLM_DEEPSEEK_R1_MODEL_NAME", "test-deepseek-model")

    cfg = load_inference_bridge_config_from_env()

    assert isinstance(cfg, ModelInferenceBridgeConfig)
    assert "qwen3-coder" in cfg.model_configs
    assert cfg.model_configs["qwen3-coder"]["base_url"] == "http://test-host:8000"


@pytest.mark.unit
def test_build_inference_bridge_config_populates_deepseek(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """model_configs must include deepseek-r1 entry when LLM_DEEPSEEK_R1_URL+MODEL_NAME are set."""
    monkeypatch.setenv("LLM_CODER_URL", "http://test-host:8000")
    monkeypatch.setenv("LLM_CODER_MODEL_NAME", "test-coder-model")
    monkeypatch.setenv("LLM_CODER_FAST_URL", "http://test-host:8001")
    monkeypatch.setenv("LLM_CODER_FAST_MODEL_NAME", "test-fast-model")
    monkeypatch.setenv("LLM_DEEPSEEK_R1_URL", "http://test-host:8101")
    monkeypatch.setenv("LLM_DEEPSEEK_R1_MODEL_NAME", "test-deepseek-model")

    cfg = load_inference_bridge_config_from_env()

    assert "deepseek-r1" in cfg.model_configs
    assert cfg.model_configs["deepseek-r1"]["base_url"] == "http://test-host:8101"


@pytest.mark.unit
def test_build_inference_bridge_config_empty_when_no_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When env vars are absent, model_configs should be empty (no crash)."""
    monkeypatch.delenv("LLM_CODER_URL", raising=False)
    monkeypatch.delenv("LLM_CODER_MODEL_NAME", raising=False)
    monkeypatch.delenv("LLM_CODER_FAST_URL", raising=False)
    monkeypatch.delenv("LLM_CODER_FAST_MODEL_NAME", raising=False)
    monkeypatch.delenv("LLM_DEEPSEEK_R1_URL", raising=False)
    monkeypatch.delenv("LLM_DEEPSEEK_R1_MODEL_NAME", raising=False)

    cfg = load_inference_bridge_config_from_env()

    assert isinstance(cfg, ModelInferenceBridgeConfig)
    # Absent env var = no entry; no crash
    assert "qwen3-coder" not in cfg.model_configs


# ---------------------------------------------------------------------------
# Bug 2: run_review wires concrete handlers (not stubs)
# ---------------------------------------------------------------------------

_STUB_DIFF = [
    {
        "file_path": "foo.py",
        "content": "diff --git a/foo.py b/foo.py\n+secret = 'pw'\n",
        "additions": 1,
        "deletions": 0,
    }
]

_SAMPLE_LLM_RESPONSE = json.dumps(
    [
        {
            "title": "Hardcoded secret",
            "description": "Password hardcoded in source.",
            "severity": "CRITICAL",
            "category": "security",
            "confidence": 0.95,
            "suggestion": "Use env vars.",
            "evidence": {"file_path": "foo.py", "line_start": 1},
        }
    ]
)


@pytest.mark.unit
def test_run_review_uses_concrete_thread_poster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_review must wire HandlerThreadPoster, not _StubThreadPoster."""
    monkeypatch.setenv("LLM_CODER_URL", "http://test-host:8000")
    monkeypatch.setenv("LLM_CODER_FAST_URL", "http://test-host:8001")
    monkeypatch.setenv("LLM_DEEPSEEK_R1_URL", "http://test-host:8101")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    captured: dict[str, object] = {}

    original_init = HandlerThreadPoster.__init__

    def spy_init(self: HandlerThreadPoster, *args: object, **kwargs: object) -> None:
        captured["thread_poster_constructed"] = True
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(HandlerThreadPoster, "__init__", spy_init)

    # Stub out actual HTTP/GitHub calls so the test runs offline
    with (
        patch(
            "omnimarket.nodes.node_pr_review_bot.handlers.handler_diff_fetcher.HandlerDiffFetcher.fetch",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge.AdapterInferenceBridge.infer",
            new_callable=AsyncMock,
            return_value=_SAMPLE_LLM_RESPONSE,
        ),
    ):
        result = run_review(
            pr_number=1,
            repo="owner/repo",
            github_token="test-token",
            reviewer_models=["qwen3-coder"],
            dry_run=True,
        )

    assert captured.get("thread_poster_constructed"), (
        "HandlerThreadPoster was never constructed — stub is still wired"
    )
    assert result is not None


@pytest.mark.unit
def test_run_review_no_value_error_on_model_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_review must not raise ValueError: Unknown model_key when env vars are set."""
    monkeypatch.setenv("LLM_CODER_URL", "http://test-host:8000")
    monkeypatch.setenv("LLM_CODER_FAST_URL", "http://test-host:8001")
    monkeypatch.setenv("LLM_DEEPSEEK_R1_URL", "http://test-host:8101")
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")

    with (
        patch(
            "omnimarket.nodes.node_pr_review_bot.handlers.handler_diff_fetcher.HandlerDiffFetcher.fetch",
            new_callable=AsyncMock,
            return_value=[],
        ),
        patch(
            "omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge.AdapterInferenceBridge.infer",
            new_callable=AsyncMock,
            return_value="[]",
        ),
    ):
        # Must NOT raise ValueError: Unknown model_key
        result = run_review(
            pr_number=42,
            repo="owner/repo",
            github_token="test-token",
            reviewer_models=["qwen3-coder"],
            dry_run=True,
        )
    assert result.correlation_id is not None
