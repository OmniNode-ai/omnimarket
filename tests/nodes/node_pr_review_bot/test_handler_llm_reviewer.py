# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""TDD tests for HandlerLlmReviewer (OMN-8446).

Tests assert:
1. Real LLM call is made (mocked) with correct prompt structure
2. Response is parsed into ReviewFinding shape (non-empty, non-stub)
3. Model selection reads from contract inputs (not hardcoded)
4. Fallback: when local LLM is unreachable, falls back gracefully
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from omnimarket.nodes.node_pr_review_bot.handlers.handler_llm_reviewer import (
    HandlerLlmReviewer,
    LlmReviewerConfig,
)
from omnimarket.nodes.node_pr_review_bot.models.models import (
    DiffHunk,
    EnumFindingSeverity,
    ReviewFinding,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_SAMPLE_DIFF_CONTENT = (
    "diff --git a/foo.py b/foo.py\n+secret = 'hardcoded_password_123'\n"
)

_SAMPLE_LLM_RESPONSE = json.dumps(
    [
        {
            "category": "security",
            "severity": "critical",
            "title": "Hardcoded password",
            "description": "Hardcoded credential 'hardcoded_password_123' found in foo.py.",
            "confidence": "high",
        }
    ]
)


def _make_hunk(content: str = _SAMPLE_DIFF_CONTENT) -> DiffHunk:
    return DiffHunk(
        file_path="foo.py",
        start_line=1,
        end_line=3,
        content=content,
    )


def _make_config(reviewer_models: list[str] | None = None) -> LlmReviewerConfig:
    return LlmReviewerConfig(
        reviewer_models=reviewer_models or ["qwen3-coder-30b"],
        model_context_windows={"qwen3-coder-30b": 32_000, "qwen3-14b": 32_000},
        timeout_seconds=30.0,
    )


# ---------------------------------------------------------------------------
# Test 1: real review text produced (not empty stub output)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pr_review_bot_produces_real_review_text() -> None:
    """HandlerLlmReviewer must return non-empty, non-stub findings.

    DoD: test_pr_review_bot_produces_real_review_text from OMN-8446.
    """
    mock_infer = AsyncMock(return_value=_SAMPLE_LLM_RESPONSE)

    with patch(
        "omnimarket.nodes.node_pr_review_bot.handlers.handler_llm_reviewer"
        ".AdapterInferenceBridge"
    ) as mock_bridge:
        instance = mock_bridge.return_value
        instance.infer = mock_infer

        reviewer = HandlerLlmReviewer(config=_make_config())
        findings = reviewer.review(
            correlation_id=uuid4(),
            diff_hunks=((_make_hunk()),),
            reviewer_models=["qwen3-coder-30b"],
        )

    assert len(findings) > 0, "Expected at least one finding — stub returns empty list"
    assert all(isinstance(f, ReviewFinding) for f in findings)
    assert findings[0].title == "Hardcoded password"
    assert findings[0].severity == EnumFindingSeverity.CRITICAL
    assert findings[0].source_model == "qwen3-coder-30b"


# ---------------------------------------------------------------------------
# Test 2: model_routing declared in contract.yaml
# ---------------------------------------------------------------------------


def test_pr_review_bot_uses_model_routing() -> None:
    """contract.yaml must declare model_routing for the reviewer role."""
    import importlib.util
    import pathlib

    import yaml

    # Resolve contract.yaml from the installed package location
    spec = importlib.util.find_spec("omnimarket.nodes.node_pr_review_bot")
    assert spec is not None, "node_pr_review_bot package not found"
    pkg_path = pathlib.Path(spec.origin).parent  # type: ignore[arg-type]
    contract_path = pkg_path / "contract.yaml"
    contract = yaml.safe_load(contract_path.read_text())
    assert "model_routing" in contract, (
        "contract.yaml missing 'model_routing' key — required by OMN-8446 DoD"
    )
    routing = contract["model_routing"]
    assert "reviewer" in routing, "model_routing must declare 'reviewer' role"


# ---------------------------------------------------------------------------
# Test 3: infer() is called with correct prompt structure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_llm_reviewer_calls_infer_with_correct_shape() -> None:
    """review() must call infer() with the adversarial_reviewer_pr prompt template."""
    mock_infer = AsyncMock(return_value=_SAMPLE_LLM_RESPONSE)

    with patch(
        "omnimarket.nodes.node_pr_review_bot.handlers.handler_llm_reviewer"
        ".AdapterInferenceBridge"
    ) as mock_bridge:
        instance = mock_bridge.return_value
        instance.infer = mock_infer

        reviewer = HandlerLlmReviewer(config=_make_config())
        reviewer.review(
            correlation_id=uuid4(),
            diff_hunks=(_make_hunk(),),
            reviewer_models=["qwen3-coder-30b"],
        )

    assert mock_infer.called, "AdapterInferenceBridge.infer() was never called"
    call_kwargs = mock_infer.call_args
    # model_key should match one of the reviewer_models
    model_key = call_kwargs.kwargs.get("model_key") or call_kwargs.args[0]
    assert model_key == "qwen3-coder-30b"
    # user_prompt must contain the diff content
    user_prompt = call_kwargs.kwargs.get("user_prompt") or call_kwargs.args[2]
    assert "foo.py" in user_prompt or "hardcoded" in user_prompt.lower(), (
        "user_prompt must embed the diff content"
    )
    # system_prompt must be non-empty
    system_prompt = call_kwargs.kwargs.get("system_prompt") or call_kwargs.args[1]
    assert len(system_prompt) > 100, "system_prompt suspiciously short"


# ---------------------------------------------------------------------------
# Test 4: fallback — local LLM unreachable returns empty findings gracefully
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_llm_reviewer_fallback_on_unreachable() -> None:
    """When the local LLM endpoint raises, review() should return empty (not crash)."""
    import httpx

    mock_infer = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

    with patch(
        "omnimarket.nodes.node_pr_review_bot.handlers.handler_llm_reviewer"
        ".AdapterInferenceBridge"
    ) as mock_bridge:
        instance = mock_bridge.return_value
        instance.infer = mock_infer

        reviewer = HandlerLlmReviewer(config=_make_config())
        findings = reviewer.review(
            correlation_id=uuid4(),
            diff_hunks=(_make_hunk(),),
            reviewer_models=["qwen3-coder-30b"],
        )

    # Must not raise; returns empty list on failure (caller handles circuit breaker)
    assert isinstance(findings, list)


# ---------------------------------------------------------------------------
# Test 5: model selection reads from config (contract inputs), not hardcoded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handler_llm_reviewer_uses_configured_models() -> None:
    """review() must call infer() for each model in reviewer_models arg."""
    call_model_keys: list[str] = []

    async def capture_infer(
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
    ) -> str:
        call_model_keys.append(model_key)
        return _SAMPLE_LLM_RESPONSE

    with patch(
        "omnimarket.nodes.node_pr_review_bot.handlers.handler_llm_reviewer"
        ".AdapterInferenceBridge"
    ) as mock_bridge:
        instance = mock_bridge.return_value
        instance.infer = capture_infer

        config = LlmReviewerConfig(
            reviewer_models=["qwen3-coder-30b", "qwen3-14b"],
            model_context_windows={"qwen3-coder-30b": 32_000, "qwen3-14b": 32_000},
            timeout_seconds=30.0,
        )
        reviewer = HandlerLlmReviewer(config=config)
        reviewer.review(
            correlation_id=uuid4(),
            diff_hunks=(_make_hunk(),),
            reviewer_models=["qwen3-coder-30b", "qwen3-14b"],
        )

    assert "qwen3-coder-30b" in call_model_keys
    assert "qwen3-14b" in call_model_keys
