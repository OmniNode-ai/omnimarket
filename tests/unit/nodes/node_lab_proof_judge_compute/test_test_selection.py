# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerTestSelection."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_lab_proof_judge_compute.handlers.handler_test_selection import (
    HandlerTestSelection,
)
from omnimarket.nodes.node_lab_proof_judge_compute.models.model_test_selection import (
    ModelTestSelectionRequest,
)
from omnimarket.nodes.node_lab_proof_judge_compute.overlay import (
    ModelLabProofJudgeOverlay,
)


@pytest.fixture
def overlay() -> ModelLabProofJudgeOverlay:
    from omnimarket.nodes.node_lab_proof_judge_compute.overlay import (
        load_example_overlay,
    )

    return load_example_overlay()


@pytest.fixture
def handler(overlay: ModelLabProofJudgeOverlay) -> HandlerTestSelection:
    return HandlerTestSelection(overlay)


def test_broker_unavailable(handler: HandlerTestSelection) -> None:
    """broker_available False -> marker_expression 'not kafka', not_run_marker_expression 'kafka'."""
    req = ModelTestSelectionRequest(
        repo="OmniNode-ai/omnimarket", broker_available=False
    )
    result = handler.handle(req)
    assert result.marker_expression == "not kafka"
    assert result.not_run_marker_expression == "kafka"
    assert result.not_run_reason != ""
    assert result.source != ""


def test_broker_available(handler: HandlerTestSelection) -> None:
    """broker_available True -> both empty."""
    req = ModelTestSelectionRequest(
        repo="OmniNode-ai/omnimarket", broker_available=True
    )
    result = handler.handle(req)
    assert result.marker_expression == ""
    assert result.not_run_marker_expression == ""


def test_unknown_repo(handler: HandlerTestSelection) -> None:
    """Unknown repo -> both empty."""
    req = ModelTestSelectionRequest(repo="unknown/repo", broker_available=False)
    result = handler.handle(req)
    assert result.marker_expression == ""
    assert result.not_run_marker_expression == ""
