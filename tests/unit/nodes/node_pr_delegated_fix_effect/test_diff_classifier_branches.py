# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Document classification refuses mixed, unreadable, and unparseable diffs."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnimarket.nodes.node_pr_delegated_fix_effect.handlers.diff_classifier import (
    DocstringCommentDiffClassifier,
    is_docstring_comment_only_change,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ("", "# comment\n", True),
        ('"""old"""\n', '"""new"""\n', True),
        ('class C:\n    "old"\n', 'class C:\n    "new"\n', True),
        ('def f():\n    "old"\n', 'def f():\n    "new"\n', True),
        ('async def f():\n    "old"\n', 'async def f():\n    "new"\n', True),
        ("def f():\n    return 1\n", "def f():\n    return 2\n", False),
        ("value = 1\n", "value = 1\n# explanation\n", True),
        ("42\n", "43\n", False),
        ("def f(:\n", "pass\n", False),
        ("pass\n", "def f(:\n", False),
    ],
)
def test_ast_verdicts(before: str, after: str, expected: bool) -> None:
    assert is_docstring_comment_only_change(before, after) is expected


@pytest.mark.parametrize("error", [ValueError("invalid source"), RecursionError()])
def test_parser_failures_refuse(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(ast, "parse", Mock(side_effect=error))
    assert not is_docstring_comment_only_change("pass", "pass")


@pytest.mark.parametrize("files", [[], ["README.md"], ["a.py", "data.json"]])
def test_non_python_or_empty_diff_never_calls_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: list[str]
) -> None:
    run = Mock()
    monkeypatch.setattr(subprocess, "run", run)
    assert not DocstringCommentDiffClassifier().is_document_class(
        tmp_path, changed_files=files
    )
    run.assert_not_called()


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [
        ("document", True),
        ("mixed", False),
        ("merge-base-failed", False),
        ("new-file", False),
        ("deleted-file", False),
        ("invalid-encoding", False),
        ("invalid-source", False),
    ],
)
def test_worktree_verdicts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    expected: bool,
) -> None:
    before = '"old"\nvalue = 1\n'
    (tmp_path / "a.py").write_text('"new"\nvalue = 1\n', encoding="utf-8")
    target = tmp_path / "b.py"
    if scenario != "deleted-file":
        target.write_text(
            "value = 2\n" if scenario == "mixed" else before, encoding="utf-8"
        )
    if scenario == "invalid-encoding":
        target.write_bytes(b"\xff")
    elif scenario == "invalid-source":
        target.write_text("def f(:\n", encoding="utf-8")
    merge_base = subprocess.CompletedProcess(
        [], 1 if scenario == "merge-base-failed" else 0, "base-sha\n", "no base"
    )
    show = subprocess.CompletedProcess([], 0, before, "")
    second_show = subprocess.CompletedProcess(
        [], 1 if scenario == "new-file" else 0, before, "missing path"
    )
    run = Mock(side_effect=[merge_base, show, second_show])
    monkeypatch.setattr(subprocess, "run", run)

    assert (
        DocstringCommentDiffClassifier("origin/main").is_document_class(
            tmp_path, changed_files=["a.py", "b.py"]
        )
        is expected
    )
    assert run.call_args_list[0].args[0] == [
        "git",
        "-C",
        str(tmp_path),
        "merge-base",
        "origin/main",
        "HEAD",
    ]
    if scenario != "merge-base-failed":
        assert run.call_args_list[2].args[0][-1] == "base-sha:b.py"


@pytest.mark.parametrize(
    "error", [OSError("git unavailable"), subprocess.TimeoutExpired("git", 30)]
)
def test_git_failures_refuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=error))
    assert not DocstringCommentDiffClassifier().is_document_class(
        tmp_path, changed_files=["a.py"]
    )
