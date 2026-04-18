# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decision table tests for HandlerPolishTaskClassifier (OMN-8986).

One test per decision table row (§3.2). No LLM calls. All deterministic.
"""

import asyncio
from uuid import UUID

import pytest

from omnimarket.enums.enum_polish_task_class import EnumPolishTaskClass
from omnimarket.nodes.node_polish_task_classifier.handlers.handler_classifier import (
    HandlerPolishTaskClassifier,
    _classify,
)
from omnimarket.nodes.node_polish_task_classifier.models.model_polish_classify_request import (
    ModelPolishClassifyRequest,
)

_CORRELATION_ID = UUID("00000000-0000-0000-0000-000000000001")
_PR = 42
_REPO = "OmniNode-ai/omnimarket"


def _req(**kwargs: object) -> ModelPolishClassifyRequest:
    return ModelPolishClassifyRequest(pr_number=_PR, repo=_REPO, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# thread_body branch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_thread_body_short_classifies_thread_reply() -> None:
    result = _classify(_req(thread_body="short comment"))
    assert result.task_class == EnumPolishTaskClass.THREAD_REPLY
    assert result.confidence == 0.8


@pytest.mark.unit
def test_thread_body_exactly_1999_chars_classifies_thread_reply() -> None:
    result = _classify(_req(thread_body="x" * 1999))
    assert result.task_class == EnumPolishTaskClass.THREAD_REPLY


@pytest.mark.unit
def test_thread_body_exactly_2000_chars_classifies_stuck() -> None:
    result = _classify(_req(thread_body="x" * 2000))
    assert result.task_class == EnumPolishTaskClass.STUCK
    assert result.confidence == 1.0
    assert "thread too long" in result.reason


@pytest.mark.unit
def test_thread_body_over_2000_chars_classifies_stuck() -> None:
    result = _classify(_req(thread_body="x" * 5000))
    assert result.task_class == EnumPolishTaskClass.STUCK


# ---------------------------------------------------------------------------
# conflict_hunk branch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_conflict_hunk_single_file_with_markers_classifies_conflict_hunk() -> None:
    hunk = "<<<<<<< HEAD\nfoo\n=======\nbar\n>>>>>>> branch"
    result = _classify(_req(conflict_hunk=hunk))
    assert result.task_class == EnumPolishTaskClass.CONFLICT_HUNK
    assert result.confidence == 0.9


@pytest.mark.unit
def test_conflict_hunk_no_markers_classifies_stuck() -> None:
    result = _classify(_req(conflict_hunk="no markers here"))
    assert result.task_class == EnumPolishTaskClass.STUCK
    assert result.confidence == 1.0
    assert "ambiguous" in result.reason or "multi-file" in result.reason


@pytest.mark.unit
def test_conflict_hunk_multi_file_classifies_stuck() -> None:
    # Two marker blocks = multi-file merged hunk
    hunk = (
        "<<<<<<< HEAD\nfoo\n=======\nbar\n>>>>>>> b\n"
        "<<<<<<< HEAD\nbaz\n=======\nqux\n>>>>>>> b"
    )
    result = _classify(_req(conflict_hunk=hunk))
    assert result.task_class == EnumPolishTaskClass.STUCK
    assert result.confidence == 1.0


# ---------------------------------------------------------------------------
# ci_log branch
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_ci_log_short_no_dep_patterns_classifies_ci_fix() -> None:
    result = _classify(_req(ci_log="ERROR: test_foo failed"))
    assert result.task_class == EnumPolishTaskClass.CI_FIX
    assert result.confidence == 0.7


@pytest.mark.unit
def test_ci_log_exactly_19999_chars_no_dep_classifies_ci_fix() -> None:
    result = _classify(_req(ci_log="x" * 19_999))
    assert result.task_class == EnumPolishTaskClass.CI_FIX


@pytest.mark.unit
def test_ci_log_exactly_20000_chars_classifies_stuck() -> None:
    result = _classify(_req(ci_log="x" * 20_000))
    assert result.task_class == EnumPolishTaskClass.STUCK
    assert result.confidence == 1.0


@pytest.mark.unit
def test_ci_log_over_20000_chars_classifies_stuck() -> None:
    result = _classify(_req(ci_log="x" * 30_000))
    assert result.task_class == EnumPolishTaskClass.STUCK


@pytest.mark.unit
@pytest.mark.parametrize(
    "dep_pattern",
    ["pyproject.toml", "uv.lock", "requirements", "package.json"],
)
def test_ci_log_dep_change_pattern_classifies_stuck(dep_pattern: str) -> None:
    log = f"Updating {dep_pattern} detected in diff"
    result = _classify(_req(ci_log=log))
    assert result.task_class == EnumPolishTaskClass.STUCK
    assert result.confidence == 1.0
    assert dep_pattern in result.reason


# ---------------------------------------------------------------------------
# Multiple signals → STUCK
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_multiple_signals_thread_and_conflict_classifies_stuck() -> None:
    result = _classify(
        _req(
            thread_body="comment",
            conflict_hunk="<<<<<<< HEAD\na\n=======\nb\n>>>>>>> x",
        )
    )
    assert result.task_class == EnumPolishTaskClass.STUCK
    assert "ambiguous" in result.reason or "multiple" in result.reason


@pytest.mark.unit
def test_multiple_signals_thread_and_ci_log_classifies_stuck() -> None:
    result = _classify(_req(thread_body="comment", ci_log="ERROR: test failed"))
    assert result.task_class == EnumPolishTaskClass.STUCK


@pytest.mark.unit
def test_multiple_signals_conflict_and_ci_log_classifies_stuck() -> None:
    hunk = "<<<<<<< HEAD\na\n=======\nb\n>>>>>>> x"
    result = _classify(_req(conflict_hunk=hunk, ci_log="test failed"))
    assert result.task_class == EnumPolishTaskClass.STUCK


@pytest.mark.unit
def test_all_three_signals_classifies_stuck() -> None:
    hunk = "<<<<<<< HEAD\na\n=======\nb\n>>>>>>> x"
    result = _classify(
        _req(thread_body="comment", conflict_hunk=hunk, ci_log="test failed")
    )
    assert result.task_class == EnumPolishTaskClass.STUCK


# ---------------------------------------------------------------------------
# No signals → STUCK
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_no_signals_classifies_stuck() -> None:
    result = _classify(_req())
    assert result.task_class == EnumPolishTaskClass.STUCK
    assert result.confidence == 1.0
    assert result.reason == "no signal"


# ---------------------------------------------------------------------------
# Handler async wrapper
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_handler_handle_delegates_to_classify() -> None:
    handler = HandlerPolishTaskClassifier()
    req = _req(thread_body="hello")
    result = asyncio.run(handler.handle(_CORRELATION_ID, req))
    assert result.task_class == EnumPolishTaskClass.THREAD_REPLY


@pytest.mark.unit
def test_handler_type_and_category() -> None:
    handler = HandlerPolishTaskClassifier()
    assert handler.handler_type == "NODE_HANDLER"
    assert handler.handler_category == "COMPUTE"
