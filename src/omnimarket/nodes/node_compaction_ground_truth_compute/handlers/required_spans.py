# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Required spans of prose items, by the compaction loss node's rule.

An identifier item carries its identifier (a row stamp, a lane, a path). A prose item
carries the stems (first six letters) of the up-to-three longest content words of its
text, filler words excluded, so a restated inflection still matches. The rule is the loss
node's, copied here because one node does not import another node's private handler;
the span-rule test pins the two against each other.
"""

from __future__ import annotations

import re

_WORD = re.compile(r"[a-z][a-z0-9-]{3,}")
_STOPWORDS = frozenset(
    [
        "that",
        "this",
        "with",
        "from",
        "have",
        "will",
        "what",
        "when",
        "then",
        "than",
        "they",
        "them",
        "their",
        "there",
        "these",
        "those",
        "into",
        "onto",
        "just",
        "also",
        "very",
        "been",
        "were",
        "your",
        "about",
        "would",
        "could",
        "should",
        "after",
        "before",
        "against",
        "otherwise",
        "available",
        "anything",
        "something",
        "everything",
        "becomes",
        "become",
        "until",
        "while",
        "where",
        "which",
        "other",
        "every",
        "each",
        "more",
        "most",
        "some",
        "such",
        "only",
        "once",
    ]
)
_STEM = 6


def content_spans(text: str) -> tuple[str, ...]:
    """Stems of the up-to-three longest content words of ``text``."""
    words = {w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS}
    longest = sorted(words, key=lambda w: (-len(w), w))
    stems: list[str] = []
    for word in longest:
        stem = word[:_STEM]
        if stem not in stems:
            stems.append(stem)
    return tuple(stems[:3])
