# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for task shape feature extractor (OMN-8054).

Covers the four DoD test cases:
- test_extracts_diff_size_from_git_diff
- test_extracts_file_types
- test_extracts_novelty_score_from_ledger
- test_returns_default_shape_on_missing_context
"""

from __future__ import annotations

from omnimarket.nodes.node_routing_policy_engine.handlers.handler_task_shape_extractor import (
    EnumFileType,
    ModelTaskShapeContext,
    extract_task_shape,
)


def test_extracts_diff_size_from_git_diff() -> None:
    context = ModelTaskShapeContext(diff_lines_added=30, diff_lines_removed=10)
    result = extract_task_shape(context)
    assert result.diff_size == 40


def test_extracts_diff_size_added_only() -> None:
    context = ModelTaskShapeContext(diff_lines_added=15, diff_lines_removed=None)
    result = extract_task_shape(context)
    assert result.diff_size == 15


def test_extracts_diff_size_removed_only() -> None:
    context = ModelTaskShapeContext(diff_lines_added=None, diff_lines_removed=5)
    result = extract_task_shape(context)
    assert result.diff_size == 5


def test_extracts_file_types() -> None:
    context = ModelTaskShapeContext(
        file_paths=(
            "src/foo.py",
            "src/bar.ts",
            "contract.yaml",
            "README.md",
        )
    )
    result = extract_task_shape(context)
    assert EnumFileType.PYTHON in result.file_types
    assert EnumFileType.TYPESCRIPT in result.file_types
    assert EnumFileType.YAML in result.file_types
    assert EnumFileType.MARKDOWN in result.file_types


def test_extracts_file_types_unknown_extension() -> None:
    context = ModelTaskShapeContext(file_paths=("Makefile", "src/lib.rs"))
    result = extract_task_shape(context)
    assert EnumFileType.OTHER in result.file_types


def test_extracts_file_types_tsx_is_typescript() -> None:
    context = ModelTaskShapeContext(file_paths=("src/App.tsx",))
    result = extract_task_shape(context)
    assert EnumFileType.TYPESCRIPT in result.file_types


def test_extracts_novelty_score_from_ledger() -> None:
    context = ModelTaskShapeContext(ledger_novelty_score=0.85)
    result = extract_task_shape(context)
    assert result.novelty_score == 0.85


def test_extracts_novelty_score_zero() -> None:
    context = ModelTaskShapeContext(ledger_novelty_score=0.0)
    result = extract_task_shape(context)
    assert result.novelty_score == 0.0


def test_extracts_novelty_score_one() -> None:
    context = ModelTaskShapeContext(ledger_novelty_score=1.0)
    result = extract_task_shape(context)
    assert result.novelty_score == 1.0


def test_returns_default_shape_on_missing_context() -> None:
    result = extract_task_shape(None)
    assert result.diff_size == 0
    assert result.file_types == frozenset()
    assert result.novelty_score == 0.5


def test_returns_default_shape_on_empty_context() -> None:
    context = ModelTaskShapeContext()
    result = extract_task_shape(context)
    assert result.diff_size == 0
    assert result.file_types == frozenset()
    assert result.novelty_score == 0.5


def test_novelty_defaults_to_0_5_when_absent() -> None:
    context = ModelTaskShapeContext(diff_lines_added=5)
    result = extract_task_shape(context)
    assert result.novelty_score == 0.5


def test_combined_all_fields() -> None:
    context = ModelTaskShapeContext(
        diff_lines_added=100,
        diff_lines_removed=20,
        file_paths=("src/node.py", "contract.yml"),
        ledger_novelty_score=0.3,
    )
    result = extract_task_shape(context)
    assert result.diff_size == 120
    assert EnumFileType.PYTHON in result.file_types
    assert EnumFileType.YAML in result.file_types
    assert result.novelty_score == 0.3
