# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hold a fixer push that adds files nobody asked for.

The files a push adds are the new head's files that the first reviewed head did not have. When the
ticket's contract is passed, the files it names (paths written in its text) are the scope and a file
the contract names is not growth; with no contract, or one that names no path, the first reviewed
head's file set is the scope. Each added file outside that scope must be named, together with a
finding, in the fixer comment: a logical line (a line and its indented continuation lines) that
holds the file's path or base name and a finding token. Every added file named releases the push;
any added file left unnamed holds it, with the list. The result says which source set the scope.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from omnimarket.nodes.node_review_lock_effect.models.model_review_lock import (
    EnumScopeVerdict,
    ModelScopeCheckRequest,
    ModelScopeCheckResult,
)

# A finding token: F2, C-3, finding 4, issue #5, defect 6, blocker 7, or a review comment id.
DEFAULT_FINDING_PATTERN = (
    r"(?i)(?<![A-Za-z0-9])(?:(?:finding|issue|defect|blocker|F|C|B|M|N)[-_ #:]*\d+"
    r"|(?:review[ -])?comment[ =#:-]*\d{6,}|issuecomment-\d+)(?![A-Za-z0-9])"
)
_PATH = re.compile(
    r"(?<![A-Za-z0-9_./-])((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]*[A-Za-z0-9_-])"
)
_FIRST_HEAD_SCOPE = "first-head-file-set"


def contract_paths(text: str) -> set[str]:
    """Every path-like token written in a contract's text, without a leading ``./``."""
    found = set()
    for match in _PATH.finditer(text):
        token = match.group(1).lstrip("./")
        if token:
            found.add(token.rstrip("/"))
    return found


def _logical_lines(comment: str) -> list[str]:
    lines: list[str] = []
    for raw in comment.splitlines():
        if not raw.strip():
            continue
        if raw[0].isspace() and lines:
            lines[-1] += " " + raw.strip()
        else:
            lines.append(raw.strip())
    return lines


def _mentions(line: str, path: str) -> bool:
    base = path.rsplit("/", 1)[-1]
    return any(
        re.search(rf"(?<![A-Za-z0-9_./-]){re.escape(name)}(?![A-Za-z0-9_-])", line)
        for name in (path, base)
    )


def _in_scope(path: str, scope: Iterable[str]) -> bool:
    return any(path == s or path.startswith(s + "/") for s in scope)


class HandlerScopeCheck:
    """Decide HOLD or RELEASE for one fixer push."""

    def handle(self, request: ModelScopeCheckRequest) -> ModelScopeCheckResult:
        first = set(request.first_files)
        scope_source = _FIRST_HEAD_SCOPE
        contract_scope: set[str] = set()
        notes: list[str] = []
        if request.contract_text is not None:
            contract_scope = contract_paths(request.contract_text)
            if contract_scope:
                scope_source = f"contract:{request.contract_path or 'text'}"
            else:
                notes.append(
                    "the contract names no path, so the first reviewed head's file set is the scope"
                )
        added = sorted(
            f
            for f in set(request.new_files)
            if f not in first and not _in_scope(f, contract_scope)
        )
        pattern = re.compile(request.finding_pattern or DEFAULT_FINDING_PATTERN)
        lines = _logical_lines(request.comment)
        named: dict[str, str] = {}
        for path in added:
            for line in lines:
                token = pattern.search(line)
                if token is not None and _mentions(line, path):
                    named[path] = token.group(0)
                    break
        unnamed = tuple(f for f in added if f not in named)
        verdict = EnumScopeVerdict.HOLD if unnamed else EnumScopeVerdict.RELEASE
        if unnamed:
            notes.append(
                f"{len(unnamed)} added file(s) outside the scope name no finding in the comment"
            )
        return ModelScopeCheckResult(
            verdict=verdict,
            scope_source=scope_source,
            added_files=tuple(added),
            unnamed_files=unnamed,
            named_files=named,
            detail="; ".join(notes),
        )
