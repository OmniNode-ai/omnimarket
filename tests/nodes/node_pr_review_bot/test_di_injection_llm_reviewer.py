# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""DI injection-path tests for OMN-10752.

Verifies that HandlerLlmReviewer accepts an injected AdapterInferenceBridge
and uses it instead of constructing one internally.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from omnimarket.nodes.node_pr_review_bot.handlers.handler_llm_reviewer import (
    HandlerLlmReviewer,
    LlmReviewerConfig,
)
from omnimarket.nodes.node_pr_review_bot.models.models import DiffHunk

_SAMPLE_RESPONSE = '[{"category": "security", "severity": "minor", "title": "Test finding", "description": "desc", "confidence": "medium"}]'


def _make_hunk() -> DiffHunk:
    return DiffHunk(file_path="foo.py", start_line=1, end_line=2, content="+ x = 1")


def _make_config() -> LlmReviewerConfig:
    return LlmReviewerConfig(
        reviewer_models=["qwen3-coder-30b"],
        model_context_windows={"qwen3-coder-30b": 32_000},
        timeout_seconds=30.0,
    )


@pytest.mark.unit
def test_llm_reviewer_accepts_injected_bridge() -> None:
    """Injected bridge is used; no internal AdapterInferenceBridge construction."""
    mock_bridge = MagicMock()
    mock_bridge.infer = AsyncMock(return_value=_SAMPLE_RESPONSE)

    reviewer = HandlerLlmReviewer(config=_make_config(), bridge=mock_bridge)
    reviewer.review(
        correlation_id=uuid4(),
        diff_hunks=(_make_hunk(),),
        reviewer_models=["qwen3-coder-30b"],
    )

    assert mock_bridge.infer.called, "Injected bridge.infer() must be called"


@pytest.mark.unit
def test_llm_reviewer_default_bridge_constructed_without_explicit_injection() -> None:
    """Without injection, reviewer is created without raising (default bridge path)."""
    reviewer = HandlerLlmReviewer(config=_make_config())
    # Just verify construction succeeds; we do not call infer() as it needs LLM infra
    assert reviewer is not None
