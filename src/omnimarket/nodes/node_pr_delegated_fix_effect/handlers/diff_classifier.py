# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Docstring/comment-only change classifier for the Slice 1 route (OMN-16868).

The node contract routes ONLY "docstring/comment diffs" to the delegated
``task_type="document"`` path; everything else stays on the Slice 0
deterministic ruff path. This module owns that decision.

The test is AST-equivalence modulo docstrings: parse both revisions, delete
every docstring node, and compare the dumps. Comments never reach the AST at
all, so two revisions whose docstring-stripped trees are identical can differ
ONLY in comments and docstrings — which is exactly the predicate the contract
names. A logic edit smuggled alongside a docstring rewrite changes the stripped
tree and is refused.

Refusal is the default, matching the eligibility gate's posture: a syntax
error, a non-Python path, an unreadable file, or any git failure returns
``False`` and the caller falls back to the deterministic path. A wrong "yes"
here spends an LLM call and risks a logic-bearing diff; a wrong "no" costs
nothing but a ruff run.
"""

from __future__ import annotations

import ast
import logging
import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

_DOCSTRING_PARENTS = (
    ast.Module,
    ast.ClassDef,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
)


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    """Delete every docstring node in-place and return the tree."""
    for node in ast.walk(tree):
        if not isinstance(node, _DOCSTRING_PARENTS):
            continue
        body = node.body
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return tree


def is_docstring_comment_only_change(before: str, after: str) -> bool:
    """Return True iff ``before`` -> ``after`` changes only docstrings/comments.

    Both revisions must parse. Any ``SyntaxError`` returns False — an
    unparseable revision cannot be proven docstring-only, and the delegated fix
    must never guess in the permissive direction.
    """
    try:
        before_tree = _strip_docstrings(ast.parse(before))
        after_tree = _strip_docstrings(ast.parse(after))
    except (SyntaxError, ValueError, RecursionError) as exc:
        logger.debug("diff_classifier: refusing unparseable revision: %s", exc)
        return False
    return ast.dump(before_tree) == ast.dump(after_tree)


@runtime_checkable
class ProtocolDiffClassifier(Protocol):
    """Seam deciding whether a PR's diff is document-class."""

    def is_document_class(self, worktree: Path, *, changed_files: list[str]) -> bool:
        """Return True when every changed file is a docstring/comment-only edit."""
        ...


class DocstringCommentDiffClassifier:
    """Default classifier: compare the worktree against its merge-base.

    ``base_ref`` defaults to ``origin/dev``, the branch every repo in this
    registry integrates on. The comparison is per changed file, and a single
    non-qualifying file refuses the WHOLE change — a mixed diff is not
    document-class.
    """

    def __init__(self, base_ref: str = "origin/dev") -> None:
        self._base_ref = base_ref

    def is_document_class(self, worktree: Path, *, changed_files: list[str]) -> bool:
        if not changed_files:
            return False
        if not all(path.endswith(".py") for path in changed_files):
            return False
        try:
            merge_base = subprocess.run(
                ["git", "-C", str(worktree), "merge-base", self._base_ref, "HEAD"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if merge_base.returncode != 0:
                logger.debug(
                    "diff_classifier: no merge-base against %s: %s",
                    self._base_ref,
                    merge_base.stderr.strip(),
                )
                return False
            base_sha = merge_base.stdout.strip()
            for rel_path in changed_files:
                show = subprocess.run(
                    ["git", "-C", str(worktree), "show", f"{base_sha}:{rel_path}"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if show.returncode != 0:
                    # New file — nothing to compare against; not document-class.
                    return False
                head_file = worktree / rel_path
                if not head_file.is_file():
                    return False
                if not is_docstring_comment_only_change(
                    show.stdout, head_file.read_text(encoding="utf-8")
                ):
                    return False
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
            logger.debug("diff_classifier: refusing after git/read failure: %s", exc)
            return False
        return True


__all__: list[str] = [
    "DocstringCommentDiffClassifier",
    "ProtocolDiffClassifier",
    "is_docstring_comment_only_change",
]
