# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Unit tests for the shared mypy parser (ADK eval spike, P4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnimarket.experiments.adk_eval.tools.mypy_parser import (
    EnumDebtCategory,
    EnumLintSeverity,
    ModelMypyFinding,
    categorize,
    parse_mypy_jsonl,
)

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "mypy_sample.jsonl"


@pytest.mark.unit
class TestParseMypyJsonl:
    def test_fixture_parses(self) -> None:
        findings = parse_mypy_jsonl(FIXTURE_PATH.read_text())
        assert len(findings) == 6
        assert all(isinstance(f, ModelMypyFinding) for f in findings)

    def test_sorted_by_file_then_line(self) -> None:
        findings = parse_mypy_jsonl(FIXTURE_PATH.read_text())
        pairs = [(f.file, f.line) for f in findings]
        assert pairs == sorted(pairs)

    def test_column_negative_becomes_none(self) -> None:
        # mypy emits column=-1 when unknown; parser should coerce to None.
        findings = parse_mypy_jsonl(FIXTURE_PATH.read_text())
        unused_ignore = next(f for f in findings if f.error_code == "unused-ignore")
        assert unused_ignore.column is None

    def test_note_severity_preserved(self) -> None:
        findings = parse_mypy_jsonl(FIXTURE_PATH.read_text())
        notes = [f for f in findings if f.severity == EnumLintSeverity.NOTE]
        assert len(notes) == 1
        assert notes[0].error_code == "note"  # parser default when mypy gives null code

    def test_malformed_line_raises(self) -> None:
        with pytest.raises(ValueError, match="Malformed mypy JSON"):
            parse_mypy_jsonl("not valid json\n")

    def test_empty_input_returns_empty(self) -> None:
        assert parse_mypy_jsonl("") == []

    def test_single_valid_record(self) -> None:
        line = json.dumps(
            {
                "file": "a.py",
                "line": 3,
                "column": 2,
                "message": "hi",
                "hint": None,
                "code": "no-untyped-def",
                "severity": "error",
            }
        )
        findings = parse_mypy_jsonl(line + "\n")
        assert len(findings) == 1
        assert findings[0].file == "a.py"
        assert findings[0].severity == EnumLintSeverity.ERROR


@pytest.mark.unit
class TestCategorize:
    def _finding(self, code: str, msg: str) -> ModelMypyFinding:
        return ModelMypyFinding(
            file="a.py",
            line=1,
            column=1,
            severity=EnumLintSeverity.ERROR,
            error_code=code,
            message=msg,
        )

    def test_any_return(self) -> None:
        f = self._finding(
            "no-any-return", 'Returning Any from function declared to return "T"'
        )
        assert categorize(f) == EnumDebtCategory.ANY_USAGE

    def test_missing_return(self) -> None:
        f = self._finding(
            "no-untyped-def", "Function is missing a return type annotation"
        )
        assert categorize(f) == EnumDebtCategory.MISSING_RETURN

    def test_missing_arg_annotation(self) -> None:
        f = self._finding("no-untyped-def", "Function is missing a type annotation")
        assert categorize(f) == EnumDebtCategory.MISSING_ANNOTATION

    def test_dict_any(self) -> None:
        f = self._finding(
            "arg-type",
            'Argument 1 to "process" has incompatible type "dict[str, Any]"; expected "ModelFoo"',
        )
        assert categorize(f) == EnumDebtCategory.DICT_ANY

    def test_unrelated(self) -> None:
        f = self._finding("unused-ignore", 'Unused "type: ignore" comment')
        assert categorize(f) is None
