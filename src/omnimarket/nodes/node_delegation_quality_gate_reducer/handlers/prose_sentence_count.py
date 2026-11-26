# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Sentence counting for a prompt's sentence ceiling."""

from __future__ import annotations

import re

_CODE_SPAN_RE = re.compile(r"`[^`\n]*`")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")
# A sentence ends at . ! or ? (plus closing quotes or brackets), then whitespace,
# then the start of another sentence. A dot inside a version, a file name or a
# number is not followed by whitespace, and "e.g. foo" continues in lower case.
_SENTENCE_BOUNDARY_RE = re.compile(
    r"""(?<=[.!?])["')\]*_]*\s+(?=["'(\[`*_]*[A-Z0-9])"""
)
_SENTENCE_ABBREVIATION_RE = re.compile(r"(?:\be\.g|\bi\.e|\bvs|\bcf|\betc)\.$", re.I)


def count_prose_sentences(content: str) -> int:
    """Count sentences the way the limit is read: a list item is one sentence.

    A semicolon run-on is one sentence, a soft-wrapped line continues its
    paragraph, and code spans never end one. This is a count for a ceiling, not
    a parser: the 120-answer calibration set agrees with a careful reading on
    every answer's side of its limit.
    """
    text = _CODE_SPAN_RE.sub("X", content)
    items: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        current: str | None = None
        for line in block.splitlines():
            if not line.strip():
                continue
            if _LIST_ITEM_RE.match(line) or line.lstrip().startswith("#"):
                if current is not None:
                    items.append(current)
                current = _LIST_ITEM_RE.sub("", line).strip().lstrip("#").strip()
            else:
                current = (
                    line.strip() if current is None else f"{current} {line.strip()}"
                )
        if current is not None:
            items.append(current)
    count = 0
    for item in items:
        if not item:
            continue
        count += 1 + sum(
            1
            for match in _SENTENCE_BOUNDARY_RE.finditer(item)
            if not _SENTENCE_ABBREVIATION_RE.search(item[: match.start()])
        )
    return count
