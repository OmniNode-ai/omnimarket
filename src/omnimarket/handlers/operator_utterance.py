# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared deterministic utterance primitives used by capture and the tracked-item fold."""

from __future__ import annotations

import hashlib
import re

from omnimarket.models.operator_capture import EnumUtteranceKind

_SENTENCE = re.compile(r"(?:[^.!?\n]|\.(?=\d))+(?:[.!?]+|$)")
_WORD = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "can",
        "could",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "i",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "let",
        "lets",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "or",
        "our",
        "please",
        "should",
        "so",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "this",
        "to",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "yes",
        "also",
        "all",
        "any",
        "some",
        "about",
        "need",
        "needs",
        "want",
        "wants",
        "get",
        "got",
        "make",
        "sure",
        "okay",
        "ok",
    ]
)

_ASK_START = re.compile(
    r"^\s*(?:please\s+)?(?:fix|build|create|make|add|remove|delete|check|run|look|investigate|"
    r"write|move|set|update|merge|ship|dispatch|file|send|draft|find|figure|start|stop|turn|"
    r"open|close|land|review|test|port|record|track|clean|wire|install|deploy|restart|ask|"
    r"tell|give|show|put|use|go)\b",
    re.IGNORECASE,
)
_ASK_PHRASE = re.compile(
    r"\b(?:can you|could you|would you|can we|could we|please|i need you to|i want you to|we need to|"
    r"need to get|go ahead and|make sure)\b",
    re.IGNORECASE,
)
_PREFERENCE = re.compile(
    r"\b(?:from now on|always|never|every time|i only want|i want|i prefer|i'd prefer|"
    r"i would prefer|i don't want|i do not want|i like|going forward|by default)\b",
    re.IGNORECASE,
)
_DECISION = re.compile(
    r"\b(?:agree|agreed|let's go with|lets go with|go with|decided|decision|approved|"
    r"approve|should be|should not|shouldn't|must|needs to be|will be|is fine|that's fine|"
    r"we will|we'll|option [a-d1-9]|number (?:one|two|three)|do it|don't|hold|ship it)\b",
    re.IGNORECASE,
)
_DECISION_START = re.compile(
    r"^\s*(?:yes|no|nope|approved|agreed)\b[,.!]?\s+\S", re.IGNORECASE
)
_IDEA = re.compile(
    r"\b(?:what if|maybe we|maybe|idea|we could|might be worth|it would be (?:nice|great|cool)|"
    r"consider|eventually|someday|at some point)\b",
    re.IGNORECASE,
)
_STATUS_START = re.compile(
    r"^\s*(?:what|where|is|are|did|does|do|has|have|why|how|which|when|who|status)\b",
    re.IGNORECASE,
)


def item_id(capture_id: str, quote: str) -> str:
    """Stable id of one item: the same words in the same capture always get the same id."""
    return "cap-" + hashlib.sha256(f"{capture_id}\0{quote}".encode()).hexdigest()[:10]


def subject_terms(text: str, limit: int = 6) -> tuple[str, ...]:
    """Content words in order of first use, stopwords removed; the drift check's vocabulary."""
    seen: list[str] = []
    for word in _WORD.findall(text.lower()):
        if word in _STOP or (len(word) < 2 and not word.isdigit()):
            continue
        if word not in seen:
            seen.append(word)
        if len(seen) >= limit:
            break
    return tuple(seen)


_LEAD = re.compile(
    r"^\s*(?:(?:also|again|and|then|so|oh|ok|okay|plus|but|now|next|well)\b[,\s]*)+",
    re.IGNORECASE,
)


def heuristic_kind(sentence: str) -> EnumUtteranceKind:
    s = _LEAD.sub("", sentence.strip()) or sentence.strip()
    if _PREFERENCE.search(s):
        return EnumUtteranceKind.PREFERENCE
    if s.endswith("?"):
        if _ASK_PHRASE.search(s):
            return EnumUtteranceKind.ASK
        if _STATUS_START.search(s):
            return EnumUtteranceKind.STATUS_QUESTION
    if _ASK_START.search(s) or _ASK_PHRASE.search(s):
        return EnumUtteranceKind.ASK
    if _IDEA.search(s):
        return EnumUtteranceKind.IDEA
    if _DECISION_START.search(s) or _DECISION.search(s):
        return EnumUtteranceKind.DECISION
    return EnumUtteranceKind.CHAT


def heuristic_sentences(
    text: str, *, include_chat: bool = False
) -> tuple[tuple[str, EnumUtteranceKind], ...]:
    """Each sentence of a message that the wording classifies as something other than chat."""
    found: list[tuple[str, EnumUtteranceKind]] = []
    for match in _SENTENCE.finditer(text):
        quote = match.group(0).strip()
        words = len(_WORD.findall(quote.lower()))
        if words < 1:
            continue
        kind = heuristic_kind(quote)
        if kind is EnumUtteranceKind.CHAT and not include_chat:
            continue
        if words < 2 and kind is not EnumUtteranceKind.DECISION and not include_chat:
            continue
        found.append((quote, kind))
    return tuple(found)
