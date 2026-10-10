# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerUtteranceClassify: what one operator message says, item by item (OMN-20905).

Pure and deterministic. The effect hands in the message and, when the delegated local model
answered, its raw answer; this handler decides what to believe:

* a machine injection (a task notification, workflow harness text, a wake or webhook payload,
  a slash-command echo, a system reminder) is not the operator speaking and yields no item;
* a delegated answer is accepted only whole: one JSON object, every item of a known kind, and
  every quote an exact substring of the message. Anything else is discarded with the reason, and
  the deterministic fallback below classifies the message instead. A half-read answer would look
  like a classification and silently drop words, which is the failure this node exists to stop;
* the fallback splits the message into sentences and classifies each by its wording, so a
  message is never lost because the model was down.

``drop ask-<id>`` (or ``done ask-<id>``) in a message is the operator closing an open ask by
hand; the ids are returned as ``drops``.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from omnimarket.models.operator_capture import (
    EnumClassifierSource,
    EnumPromptOrigin,
    EnumUtteranceKind,
    ModelUtterance,
    ModelUtteranceClassification,
    ModelUtteranceClassifyRequest,
    ModelUtteranceItem,
)

# Text a machine put in the prompt slot. Matched on the stripped start of the message.
MACHINE_PREFIXES: tuple[str, ...] = (
    "<task-notification",
    "<command-name>",
    "<command-message>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
    "<system-reminder>",
    "<wake",
    "<webhook-payload",
    "<child-session-event",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<user-prompt-submit-hook>",
    "<scheduled-task",
    "[workflow harness",
    "[request interrupted",
    "caveat: the messages below were generated",
    "this session is being continued from a previous conversation",
)

RESPONSE_CONTRACT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "quote", "subject", "confidence"],
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [k.value for k in EnumUtteranceKind],
                    },
                    "quote": {"type": "string"},
                    "subject": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                },
            },
        }
    },
}

_INSTRUCTION = (
    "Classify the operator message below. Split it into items; each item is one of: "
    "decision (the operator settles a choice or answers a question), "
    "ask (the operator wants something done), "
    "idea (a possibility to consider later, not yet decided), "
    "preference (how the operator wants things done from now on), "
    "status_question (asks what the state of something is), "
    "chat (anything else: thanks, acknowledgement, venting with no request). "
    "For each item return kind, quote (an exact, unedited substring of the message), "
    "subject (3 to 6 lowercase words naming the topic), and confidence from 0 to 1. "
    'Answer with one JSON object {"items": [...]} and nothing else.'
)

_DROP = re.compile(r"\b(?:drop|done|close)\s+(ask-[0-9a-f]{10})\b", re.IGNORECASE)
_SENTENCE = re.compile(r"[^.!?\n]+(?:[.!?]+|$)")
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
    r"\b(?:can you|could you|would you|please|i need you to|i want you to|we need to|"
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


def is_machine_injection(text: str) -> bool:
    head = text.lstrip().lower()
    return not head or any(head.startswith(p) for p in MACHINE_PREFIXES)


def build_classify_prompt(text: str) -> str:
    """The delegated request: the instruction, then the message verbatim."""
    return f"{_INSTRUCTION}\n\nMESSAGE:\n{text}\n"


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


def _heuristic_items(utterance: ModelUtterance) -> tuple[ModelUtteranceItem, ...]:
    items: list[ModelUtteranceItem] = []
    for match in _SENTENCE.finditer(utterance.text):
        quote = match.group(0).strip()
        if len(_WORD.findall(quote.lower())) < 2:
            continue
        kind = heuristic_kind(quote)
        if kind is EnumUtteranceKind.CHAT:
            continue
        items.append(
            ModelUtteranceItem(
                item_id=item_id(utterance.capture_id, quote),
                kind=kind,
                quote=quote,
                subject=" ".join(subject_terms(quote, 5)),
                confidence=0.5,
            )
        )
    return tuple(items)


def _delegated_items(
    utterance: ModelUtterance, response: str
) -> tuple[tuple[ModelUtteranceItem, ...], str | None]:
    """The model's items, or (empty, why the answer was discarded)."""
    body = response.strip()
    fence = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", body, re.DOTALL)
    if fence:
        body = fence.group(1)
    try:
        parsed: object = json.loads(body)
    except ValueError as exc:
        return (), f"delegated answer is not JSON ({exc.__class__.__name__})"
    if not isinstance(parsed, dict) or set(parsed) != {"items"}:
        return (), "delegated answer is not one object with exactly the key items"
    raw_items = parsed["items"]
    if not isinstance(raw_items, list):
        return (), "delegated items is not a list"
    kinds = {k.value for k in EnumUtteranceKind}
    items: list[ModelUtteranceItem] = []
    for raw in raw_items:
        if not isinstance(raw, dict) or set(raw) != {
            "kind",
            "quote",
            "subject",
            "confidence",
        }:
            return (
                (),
                "a delegated item does not carry exactly kind, quote, subject, confidence",
            )
        kind, quote, subject, confidence = (
            raw["kind"],
            raw["quote"],
            raw["subject"],
            raw["confidence"],
        )
        if kind not in kinds:
            return (), f"a delegated item has unknown kind {kind!r}"
        if (
            not isinstance(quote, str)
            or not quote.strip()
            or quote not in utterance.text
        ):
            return (), "a delegated quote is not an exact substring of the message"
        if not isinstance(subject, str):
            return (), "a delegated subject is not text"
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, int | float)
            or not 0 <= confidence <= 1
        ):
            return (), "a delegated confidence is not a number from 0 to 1"
        if kind == EnumUtteranceKind.CHAT.value:
            continue
        items.append(
            ModelUtteranceItem(
                item_id=item_id(utterance.capture_id, quote),
                kind=EnumUtteranceKind(kind),
                quote=quote.strip(),
                subject=" ".join(subject.lower().split())[:80],
                confidence=float(confidence),
            )
        )
    return tuple(items), None


class HandlerUtteranceClassify:
    """Classify one operator message; never lose one because the model could not answer."""

    def handle(
        self, request: ModelUtteranceClassifyRequest
    ) -> ModelUtteranceClassification:
        utterance = request.utterance
        if is_machine_injection(utterance.text):
            return ModelUtteranceClassification(
                capture_id=utterance.capture_id,
                origin=EnumPromptOrigin.MACHINE,
                classifier=EnumClassifierSource.HEURISTIC,
                reason="machine injection: not the operator's words",
            )
        drops = tuple(dict.fromkeys(m.lower() for m in _DROP.findall(utterance.text)))
        if request.delegated_response is not None:
            items, problem = _delegated_items(utterance, request.delegated_response)
            if problem is None:
                return ModelUtteranceClassification(
                    capture_id=utterance.capture_id,
                    origin=EnumPromptOrigin.OPERATOR,
                    classifier=EnumClassifierSource.DELEGATED,
                    classifier_model=request.delegated_model,
                    reason="delegated answer accepted",
                    items=items,
                    drops=drops,
                )
            reason = f"fallback: {problem}"
        else:
            reason = f"fallback: {request.delegated_error or 'no delegated answer'}"
        return ModelUtteranceClassification(
            capture_id=utterance.capture_id,
            origin=EnumPromptOrigin.OPERATOR,
            classifier=EnumClassifierSource.HEURISTIC,
            reason=reason[:300],
            items=_heuristic_items(utterance),
            drops=drops,
        )


__all__ = [
    "MACHINE_PREFIXES",
    "RESPONSE_CONTRACT",
    "HandlerUtteranceClassify",
    "build_classify_prompt",
    "heuristic_kind",
    "is_machine_injection",
    "item_id",
    "subject_terms",
]
