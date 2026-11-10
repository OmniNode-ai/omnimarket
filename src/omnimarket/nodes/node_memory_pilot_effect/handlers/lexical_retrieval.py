# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The frozen lexical baseline retrieval (stage-one bar, systems.baseline).

A deterministic stand-in for the hand-run internal knowledge-base lookup: rank
files by distinct question terms matched, then take the best paragraphs of the
best files up to a character budget. Nothing here calls a model.
"""

from __future__ import annotations

import re

from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot import (
    ModelMemoryPilotContextRef,
    ModelMemoryPilotDocument,
)

STOP_WORDS = frozenset(
    [
        "the",
        "and",
        "for",
        "what",
        "which",
        "who",
        "how",
        "why",
        "when",
        "does",
        "did",
        "are",
        "was",
        "were",
        "with",
        "that",
        "this",
        "from",
        "into",
        "than",
        "then",
        "its",
        "their",
        "has",
        "have",
        "had",
        "not",
        "can",
        "may",
        "any",
        "all",
        "one",
        "two",
    ]
)
TOP_FILES = 5
CONTEXT_CHARS = 6000
_WORD = re.compile(r"[a-z0-9]+")


def question_terms(question: str) -> tuple[str, ...]:
    """Lowercased tokens of three or more characters that are not stop words, in order, once each."""
    seen: dict[str, None] = {}
    for token in _WORD.findall(question.lower()):
        if len(token) >= 3 and token not in STOP_WORDS:
            seen.setdefault(token)
    return tuple(seen)


def _score(text: str, terms: tuple[str, ...]) -> tuple[int, int]:
    words = _WORD.findall(text.lower())
    distinct = 0
    hits = 0
    for term in terms:
        n = sum(1 for w in words if w.startswith(term))
        if n:
            distinct += 1
            hits += n
    return distinct, hits


def retrieve(
    documents: tuple[ModelMemoryPilotDocument, ...], question: str
) -> tuple[ModelMemoryPilotContextRef, ...]:
    """Paragraphs of the top files, best first, clipped to the character budget."""
    terms = question_terms(question)
    if not terms:
        return ()
    ranked_files = []
    for doc in documents:
        distinct, hits = _score(doc.text, terms)
        if distinct:
            ranked_files.append((-distinct, -hits, doc.doc_id, doc))
    ranked_files.sort(key=lambda row: row[:3])
    refs: list[ModelMemoryPilotContextRef] = []
    budget = CONTEXT_CHARS
    for _d, _h, _id, doc in ranked_files[:TOP_FILES]:
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", doc.text) if p.strip()]
        ranked = []
        for index, para in enumerate(paragraphs):
            distinct, hits = _score(para, terms)
            if distinct:
                ranked.append((-distinct, -hits, index, para))
        ranked.sort(key=lambda row: row[:3])
        for _pd, _ph, _pi, para in ranked:
            if budget <= 0:
                return tuple(refs)
            clipped = para[:budget]
            refs.append(ModelMemoryPilotContextRef(source=doc.doc_id, excerpt=clipped))
            budget -= len(clipped)
    return tuple(refs)


def render_context(refs: tuple[ModelMemoryPilotContextRef, ...]) -> str:
    """The context text the answering model sees: each paragraph under its path."""
    return "\n\n".join(f"[{ref.source}]\n{ref.excerpt}" for ref in refs)
