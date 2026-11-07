# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which mentions of the ledger file's name or path variable are executable.

A comment, a docstring, a document and a ruling's line citation (the name directly followed by
``:<line>``) are no reader and no writer. Every other line that names the file or its path variable
is one: a write when a writer marker is on the line, a read otherwise.
"""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import PurePosixPath

from omnimarket.nodes.node_work_ledger_file_census_effect.models.model_file_census import (
    EnumCensusKind,
    ModelCensusHit,
    ModelCensusOverlay,
)


def _suffix(path: str) -> str:
    return PurePosixPath(path).suffix.lower()


def is_skipped(path: str, overlay: ModelCensusOverlay) -> bool:
    return any(part in overlay.skip_dirs for part in PurePosixPath(path).parts[:-1])


def is_code(path: str, text: str, overlay: ModelCensusOverlay) -> bool:
    suffix = _suffix(path)
    if suffix in overlay.document_suffixes:
        return False
    return (
        suffix in overlay.code_suffixes
        or PurePosixPath(path).name in overlay.code_names
        or text.startswith("#!")
    )


def _strip_marker_comment(line: str, marker: str) -> str:
    """Cut a line at the first comment marker outside single and double quotes."""
    quote = ""
    i = 0
    while i < len(line):
        char = line[i]
        if quote:
            if char == "\\":
                i += 2
                continue
            if char == quote:
                quote = ""
        elif char in "\"'":
            quote = char
        elif line.startswith(marker, i) and (
            marker != "#" or i == 0 or line[i - 1] in " \t;|&("
        ):
            return line[:i]
        i += 1
    return line


def _python_code_lines(text: str) -> dict[int, str]:
    """Line number to the line without its comment, with docstring lines removed."""
    lines = text.splitlines()
    try:
        tree = ast.parse(text)
        comments = {
            token.start: token.string
            for token in tokenize.generate_tokens(io.StringIO(text).readline)
            if token.type == tokenize.COMMENT
        }
    except (SyntaxError, tokenize.TokenError, ValueError):
        return {n: _strip_marker_comment(s, "#") for n, s in enumerate(lines, 1)}
    docstring_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstring_lines.update(
                    range(body[0].lineno, (body[0].end_lineno or body[0].lineno) + 1)
                )
    out: dict[int, str] = {}
    for number, line in enumerate(lines, 1):
        if number in docstring_lines:
            continue
        for (row, col), _ in comments.items():
            if row == number:
                line = line[:col]
        out[number] = line
    return out


def code_lines(path: str, text: str, overlay: ModelCensusOverlay) -> dict[int, str]:
    suffix = _suffix(path)
    if suffix == ".py":
        return _python_code_lines(text)
    if suffix in overlay.slash_comment_suffixes:
        return {
            n: _strip_marker_comment(s, "//")
            for n, s in enumerate(text.splitlines(), 1)
        }
    if suffix in overlay.hash_comment_suffixes or not suffix or text.startswith("#!"):
        return {
            n: _strip_marker_comment(s, "#") for n, s in enumerate(text.splitlines(), 1)
        }
    return dict(enumerate(text.splitlines(), 1))


def _patterns(overlay: ModelCensusOverlay) -> tuple[re.Pattern[str], re.Pattern[str]]:
    names = "|".join(re.escape(n) for n in overlay.names)
    variables = "|".join(re.escape(v) for v in overlay.path_variables)
    bare = re.compile(
        rf"(?<![A-Za-z0-9_])(?:{names})(?![A-Za-z0-9_])|"
        rf"(?<![A-Za-z0-9_])(?:{variables})(?![A-Za-z0-9_])"
    )
    cited = re.compile(rf"(?<![A-Za-z0-9_])(?:{names})(?:{overlay.citation_suffix})")
    return bare, cited


def executable_hits(
    path: str, text: str, overlay: ModelCensusOverlay
) -> tuple[ModelCensusHit, ...]:
    if is_skipped(path, overlay) or not is_code(path, text, overlay):
        return ()
    bare, cited = _patterns(overlay)
    writers = [re.compile(marker) for marker in overlay.writer_markers]
    hits: list[ModelCensusHit] = []
    for number, line in code_lines(path, text, overlay).items():
        remaining = cited.sub(" ", line)
        if not bare.search(remaining):
            continue
        kind = (
            EnumCensusKind.WRITER
            if any(w.search(line) for w in writers)
            else EnumCensusKind.READER
        )
        hits.append(ModelCensusHit(path=path, line=number, kind=kind))
    return tuple(hits)
