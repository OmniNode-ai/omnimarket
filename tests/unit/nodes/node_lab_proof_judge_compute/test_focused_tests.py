# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerFocusedTests."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_lab_proof_judge_compute.handlers.handler_focused_tests import (
    HandlerFocusedTests,
)
from omnimarket.nodes.node_lab_proof_judge_compute.models.model_focused_tests import (
    ModelFocusedTestsRequest,
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
def handler(overlay: ModelLabProofJudgeOverlay) -> HandlerFocusedTests:
    return HandlerFocusedTests(overlay)


def test_not_run_lines_parsed(handler: HandlerFocusedTests) -> None:
    """Not-run lines are parsed with reasons; never counted as passed."""
    req = ModelFocusedTestsRequest(
        repo="OmniNode-ai/omnimarket",
        base_focused_ok=True,
        tests_output="tests-not-run test_a reason=skipped\ntests-not-run test_b reason=xfail\n5 passed, 2 not-run",
    )
    result = handler.handle(req)
    assert result.passed_count == 5
    assert len(result.not_run) == 2
    assert result.not_run[0].test_id == "test_a"
    assert result.not_run[0].reason == "skipped"
    assert result.not_run[1].test_id == "test_b"
    assert result.not_run[1].reason == "xfail"
    assert "test_a" in result.evidence
    assert "test_b" in result.evidence


def test_all_not_run_ok_false(handler: HandlerFocusedTests) -> None:
    """All-not-run with all_not_run_ok False -> ok False."""
    req = ModelFocusedTestsRequest(
        repo="OmniNode-ai/omnimarket",
        base_focused_ok=True,
        tests_output="tests-not-run test_a reason=skipped\n0 passed, 1 not-run",
    )
    result = handler.handle(req)
    assert result.ok is False
    assert result.passed_count == 0


def test_some_passed_plus_not_run_ok_true(handler: HandlerFocusedTests) -> None:
    """Some passed plus not-run -> ok True and evidence lists the not-run."""
    req = ModelFocusedTestsRequest(
        repo="OmniNode-ai/omnimarket",
        base_focused_ok=True,
        tests_output="tests-not-run test_a reason=skipped\n3 passed, 1 not-run",
    )
    result = handler.handle(req)
    assert result.ok is True
    assert result.passed_count == 3
    assert "test_a" in result.evidence


def test_base_focused_ok_false_stays_false(handler: HandlerFocusedTests) -> None:
    """base_focused_ok False stays False."""
    req = ModelFocusedTestsRequest(
        repo="OmniNode-ai/omnimarket",
        base_focused_ok=False,
        tests_output="5 passed",
    )
    result = handler.handle(req)
    assert result.ok is False
