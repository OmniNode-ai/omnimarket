# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression tests for OMN-9112: pr_review_bot must fail loud on bad config.

Before this fix, unknown model keys were silently swallowed into an empty
findings list, which the runner then reported as `verdict="clean"`. A silent
clean verdict on a broken reviewer is worse than a loud failure — the bot
would appear to work while reviewing nothing.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from omnimarket.nodes.node_pr_review_bot.handlers.handler_llm_reviewer import (
    HandlerLlmReviewer,
    LlmReviewerConfig,
)
from omnimarket.nodes.node_pr_review_bot.models.models import DiffHunk
from omnimarket.nodes.node_pr_review_bot.workflow_runner import (
    run_review,
)


def _make_hunk() -> DiffHunk:
    return DiffHunk(
        file_path="foo.py",
        start_line=1,
        end_line=3,
        content="diff --git a/foo.py b/foo.py\n+x = 1\n",
    )


def _make_config_for_bad_key() -> LlmReviewerConfig:
    return LlmReviewerConfig(
        reviewer_models=["unregistered-model-key-xyz"],
        model_context_windows={"unregistered-model-key-xyz": 32_000},
        timeout_seconds=30.0,
    )


def test_reviewer_raises_value_error_on_unknown_model_key() -> None:
    """When AdapterInferenceBridge.infer() raises ValueError (unknown model
    key), HandlerLlmReviewer.review() must re-raise — not swallow into an
    empty findings list."""
    mock_infer = AsyncMock(
        side_effect=ValueError("Unknown model_key: unregistered-model-key-xyz")
    )

    with patch(
        "omnimarket.nodes.node_pr_review_bot.handlers.handler_llm_reviewer"
        ".AdapterInferenceBridge"
    ) as mock_bridge:
        instance = mock_bridge.return_value
        instance.infer = mock_infer

        reviewer = HandlerLlmReviewer(config=_make_config_for_bad_key())
        with pytest.raises(ValueError, match="Unknown model_key"):
            reviewer.review(
                correlation_id=uuid4(),
                diff_hunks=(_make_hunk(),),
                reviewer_models=["unregistered-model-key-xyz"],
            )


def test_workflow_runner_raises_on_empty_reviewer_models() -> None:
    """When reviewer_models is None/empty, run_review must raise
    ValueError rather than quietly picking previously-hardcoded defaults that
    are not in the model registry."""
    with pytest.raises(ValueError, match="reviewer_models must be provided"):
        run_review(
            pr_number=1,
            repo="OmniNode-ai/test",
            reviewer_models=None,
        )


def test_workflow_runner_raises_on_explicit_empty_list() -> None:
    """Passing an empty list (not None) must raise the same ValueError."""
    with pytest.raises(ValueError, match="reviewer_models must be provided"):
        run_review(
            pr_number=1,
            repo="OmniNode-ai/test",
            reviewer_models=[],
        )
