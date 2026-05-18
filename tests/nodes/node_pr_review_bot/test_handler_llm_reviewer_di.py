# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for HandlerLlmReviewer DI injection (OMN-10748)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock
from uuid import uuid4

from omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge import (
    ModelInferenceAdapter,
)
from omnimarket.nodes.node_pr_review_bot.handlers.handler_llm_reviewer import (
    HandlerLlmReviewer,
    LlmReviewerConfig,
)
from omnimarket.nodes.node_pr_review_bot.models.models import DiffHunk, ReviewFinding

_SAMPLE_LLM_RESPONSE = json.dumps(
    [
        {
            "category": "security",
            "severity": "critical",
            "title": "Hardcoded password",
            "description": "Hardcoded credential found.",
            "confidence": "high",
        }
    ]
)

_SAMPLE_DIFF_CONTENT = (
    "diff --git a/foo.py b/foo.py\n+secret = 'hardcoded_password_123'\n"
)


class _StubBridge(ModelInferenceAdapter):
    """Test double satisfying ModelInferenceAdapter protocol."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None = None,
    ) -> str:
        self.calls.append({"model_key": model_key})
        return self._response


def _make_hunk() -> DiffHunk:
    return DiffHunk(
        file_path="foo.py", start_line=1, end_line=3, content=_SAMPLE_DIFF_CONTENT
    )


def _make_config() -> LlmReviewerConfig:
    return LlmReviewerConfig(
        reviewer_models=["qwen3-coder-30b"],
        model_context_windows={"qwen3-coder-30b": 32_000},
        timeout_seconds=30.0,
    )


def test_handler_uses_injected_bridge() -> None:
    """When bridge= is passed, HandlerLlmReviewer must use it, not construct AdapterInferenceBridge."""
    stub = _StubBridge(_SAMPLE_LLM_RESPONSE)
    reviewer = HandlerLlmReviewer(config=_make_config(), bridge=stub)

    findings = reviewer.review(
        correlation_id=uuid4(),
        diff_hunks=(_make_hunk(),),
        reviewer_models=["qwen3-coder-30b"],
    )

    assert len(findings) > 0
    assert all(isinstance(f, ReviewFinding) for f in findings)
    assert len(stub.calls) == 1
    assert stub.calls[0]["model_key"] == "qwen3-coder-30b"


def test_handler_default_bridge_constructed_from_config() -> None:
    """When bridge= is None (default), AdapterInferenceBridge is constructed from config."""
    from unittest.mock import patch

    with patch(
        "omnimarket.nodes.node_pr_review_bot.handlers.handler_llm_reviewer.AdapterInferenceBridge"
    ) as mock_cls:
        mock_instance = mock_cls.return_value
        mock_instance.infer = AsyncMock(return_value=_SAMPLE_LLM_RESPONSE)

        config = _make_config()
        reviewer = HandlerLlmReviewer(config=config)  # no bridge= kwarg

        # bridge was constructed from config.inference_bridge_config
        mock_cls.assert_called_once_with(config.inference_bridge_config)
        assert reviewer._bridge is mock_instance


def test_handler_injected_bridge_skips_config_construction() -> None:
    """When bridge= is provided, AdapterInferenceBridge constructor must NOT be called."""
    from unittest.mock import patch

    stub = _StubBridge(_SAMPLE_LLM_RESPONSE)

    with patch(
        "omnimarket.nodes.node_pr_review_bot.handlers.handler_llm_reviewer.AdapterInferenceBridge"
    ) as mock_cls:
        HandlerLlmReviewer(config=_make_config(), bridge=stub)
        mock_cls.assert_not_called()


def test_handler_injected_bridge_multiple_models() -> None:
    """Each model in reviewer_models must invoke the injected bridge once."""
    stub = _StubBridge(_SAMPLE_LLM_RESPONSE)
    config = LlmReviewerConfig(
        reviewer_models=["qwen3-coder-30b", "qwen3-14b"],
        model_context_windows={"qwen3-coder-30b": 32_000, "qwen3-14b": 32_000},
        timeout_seconds=30.0,
    )
    reviewer = HandlerLlmReviewer(config=config, bridge=stub)

    reviewer.review(
        correlation_id=uuid4(),
        diff_hunks=(_make_hunk(),),
        reviewer_models=["qwen3-coder-30b", "qwen3-14b"],
    )

    called_keys = [c["model_key"] for c in stub.calls]
    assert "qwen3-coder-30b" in called_keys
    assert "qwen3-14b" in called_keys
