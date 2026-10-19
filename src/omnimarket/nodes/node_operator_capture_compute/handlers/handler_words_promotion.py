# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerWordsPromotion: the operator's words become a RULING, a consent, or an inbox message (OMN-19513).

The capture rows record what the operator said as STATUS rows. A decision in them bound no lane
until someone copied it by hand into a RULING or OPERATOR-CONSENT row, so words were lost: a
reviewer decision never became a ruling, and a key approval sat on no row until a lane blocked
on it. This decision closes that gap from the operator's own words and the assistant reply they
answered, and nothing else.

Pure and deterministic:

* Only decision, preference and ask items are promoted; status questions and ideas stay as
  their capture rows (no row here).
* The question is a sentence of the assistant reply captured before the prompt, the last one
  ending in ``?``, taken as written. With no reply, or a reply that asks nothing, the words go to
  the orchestrator inbox: a RULING names the question it answers, and this handler never writes
  one of its own.
* A reply that proposes a consent carries its own ``APPROVED SCOPE:`` and ``OUT OF SCOPE:``
  lines. A plain yes to it is an OPERATOR-CONSENT whose scope lists are copied from the reply;
  any other answer goes to the inbox. This handler never writes a scope.
* Any other promoted item becomes a RULING when every item is classified at or above
  ``min_confidence`` or the words are a plain yes or no; otherwise the inbox. A wrong ruling binds
  lanes, so a guess is handed to the orchestrator rather than recorded.

The inbox row is a MSG to the orchestrator lane with the words verbatim, so nothing the operator
decided is dropped silently.
"""

from __future__ import annotations

import re
from datetime import UTC

from omnimarket.models.operator_capture import (
    EnumDriftRelation,
    EnumPromotionVerdict,
    EnumUtteranceKind,
    ModelUtteranceItem,
    ModelWordsPromotion,
    ModelWordsPromotionRequest,
)
from omnimarket.nodes.node_operator_capture_compute.handlers.handler_capture_rows import (
    STAMP_FORMAT,
    ledger_words,
)

PROMOTED_KINDS = frozenset(
    {EnumUtteranceKind.DECISION, EnumUtteranceKind.PREFERENCE, EnumUtteranceKind.ASK}
)
SCOPE_PREFIXES = ("APPROVED SCOPE:", "OUT OF SCOPE:")
# A message is never consent-shaped; these never appear in an inbox row.
CONSENT_MARKERS = (
    "APPROVED SCOPE",
    "OUT OF SCOPE",
    "OPERATOR-CONSENT",
    "ROTATION-CONSENT",
)
PLACEHOLDERS = frozenset(
    {"", "-", "--", ".", "none", "n/a", "na", "nil", "tbd", "nothing", "?"}
)
_YES = frozenset(
    {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "approved", "agree"}
)
_NO = frozenset({"no", "nope"})
_FILLER = frozenset(
    {
        "go",
        "ahead",
        "do",
        "it",
        "please",
        "proceed",
        "i",
        "that",
        "this",
        "sounds",
        "good",
    }
)
_WORD_RE = re.compile(r"[a-z']+")
_QUESTION_RE = re.compile(r"[^.?!\n]*\?")
_CELL_UNSAFE = re.compile(r"[|\n\r=]")


def _cell(value: str) -> str:
    return " ".join(_CELL_UNSAFE.sub(" ", value).split())


def _tokens(words: str) -> list[str]:
    return _WORD_RE.findall(words.lower())


def plain_yes(words: str) -> bool:
    """Every word is a yes word or filler, at least one is a yes word, and nothing is asked."""
    tokens = _tokens(words)
    if "?" in words or not tokens:
        return False
    return all(t in _YES or t in _FILLER for t in tokens) and any(
        t in _YES for t in tokens
    )


def plain_no(words: str) -> bool:
    tokens = _tokens(words)
    return "?" not in words and bool(tokens) and all(t in _NO for t in tokens)


def scope_lines(reply: str) -> dict[str, str]:
    """The reply's own scope lists, by prefix; a list that names nothing is absent."""
    found: dict[str, str] = {}
    for line in reply.splitlines():
        stripped = line.strip().lstrip("-* ").strip()
        for prefix in SCOPE_PREFIXES:
            if stripped.startswith(prefix):
                value = _cell(stripped[len(prefix) :])
                if value.lower().rstrip(".") not in PLACEHOLDERS:
                    found[prefix] = value.replace("|", "¦")
    return found


def question_of(reply: str | None, max_chars: int) -> str | None:
    """The last sentence of the reply that ends in a question mark, as written, or None."""
    if not reply:
        return None
    kept = "\n".join(
        ln
        for ln in reply.splitlines()
        if not ln.strip().lstrip("-* ").startswith(SCOPE_PREFIXES)
    )
    found = [
        q.strip() for q in _QUESTION_RE.findall(kept) if q.strip().strip("?").strip()
    ]
    if not found:
        return None
    question = _cell(found[-1])
    if len(question) > max_chars:
        question = question[: max_chars - 4].rstrip() + " ..."
    return question


def _quoted(items: tuple[ModelUtteranceItem, ...]) -> tuple[str, list[str]]:
    words, changed = ledger_words(" / ".join(item.quote for item in items))
    return f'"{words}"', (["verbatim=normalized"] if changed else [])


def _link_cells(
    request: ModelWordsPromotionRequest, items: tuple[ModelUtteranceItem, ...]
) -> list[str]:
    utterance = request.utterance
    cells = [
        f"session={_cell(utterance.session_id)}",
        f"capture={utterance.capture_id}",
        f"said={utterance.captured_at.astimezone(UTC).strftime(STAMP_FORMAT)}",
        f"promoted_from={','.join(item.item_id for item in items)}",
    ]
    if request.prompt_id:
        cells.append(f"capture_prompt={_cell(request.prompt_id)}")
    return cells


def inbox_row(
    request: ModelWordsPromotionRequest,
    items: tuple[ModelUtteranceItem, ...],
    reason: str,
    question: str | None,
) -> str:
    """A MSG to the orchestrator lane with the words verbatim; never consent-shaped."""
    stamp = request.stamp.strftime(STAMP_FORMAT)
    quoted, normalized = _quoted(items)
    cells = [
        stamp,
        "MSG",
        f"from={request.sender_lane}",
        f"id={stamp}-{request.sender_lane}",
        f"to={request.lane}",
        f"kind={','.join(sorted({item.kind.value for item in items}))}",
        *_link_cells(request, items),
        *normalized,
    ]
    if question:
        cells.append(f"question={question}")
    text = f"operator words need a ruling or consent decision ({reason}): {quoted}"
    for marker in CONSENT_MARKERS:
        text = text.replace(marker, marker.lower())
    cells.append(text)
    return " | ".join(cells)


def _ruling_row(
    request: ModelWordsPromotionRequest,
    items: tuple[ModelUtteranceItem, ...],
    question: str,
) -> str:
    quoted, normalized = _quoted(items)
    amends = sorted(
        {
            match.stamp
            for item in items
            if (drift := request.drift.get(item.item_id)) is not None
            for match in drift.matches
            if match.relation is EnumDriftRelation.CONTRADICTS
        }
    )
    return " | ".join(
        [
            request.stamp.strftime(STAMP_FORMAT),
            "RULING",
            f"lane={request.lane}",
            f"question={question}",
            "kind=decision",
            "approved_by=operator",
            *[f"amends={stamp}" for stamp in amends],
            *_link_cells(request, items),
            *normalized,
            quoted,
        ]
    )


def _consent_row(
    request: ModelWordsPromotionRequest,
    items: tuple[ModelUtteranceItem, ...],
    question: str,
    scopes: dict[str, str],
) -> str:
    quoted, normalized = _quoted(items)
    return " | ".join(
        [
            request.stamp.strftime(STAMP_FORMAT),
            "OPERATOR-CONSENT",
            f"lane={request.lane}",
            "approved_by=operator",
            f"question={question}",
            *_link_cells(request, items),
            *normalized,
            quoted,
            f"APPROVED SCOPE: {scopes['APPROVED SCOPE:']}",
            f"OUT OF SCOPE: {scopes['OUT OF SCOPE:']}",
        ]
    )


class HandlerWordsPromotion:
    """Decide what one operator prompt's words become; render the one row, or none."""

    def handle(self, request: ModelWordsPromotionRequest) -> ModelWordsPromotion:
        items = tuple(
            i for i in request.classification.items if i.kind in PROMOTED_KINDS
        )
        if not items:
            return ModelWordsPromotion(
                verdict=EnumPromotionVerdict.NONE,
                reason="no decision, preference or ask; the capture rows hold the words",
            )
        question = question_of(request.reply, request.max_question_chars)

        def inbox(reason: str) -> ModelWordsPromotion:
            return ModelWordsPromotion(
                verdict=EnumPromotionVerdict.INBOX,
                rows=(inbox_row(request, items, reason, question),),
                question=question,
                reason=reason,
            )

        if question is None:
            return inbox(
                "no joined question, and a ruling names the question it answers"
            )
        words = " ".join(item.quote for item in items)
        scopes = scope_lines(request.reply or "")
        if scopes:
            if len(scopes) < len(SCOPE_PREFIXES):
                return inbox("the consent proposal does not name both scope lists")
            if not plain_yes(words):
                return inbox(
                    "the words answer a consent proposal but are not a plain yes"
                )
            return ModelWordsPromotion(
                verdict=EnumPromotionVerdict.OPERATOR_CONSENT,
                rows=(_consent_row(request, items, question, scopes),),
                question=question,
                reason="a plain yes to a consent proposal that names both scope lists",
            )
        confident = all(item.confidence >= request.min_confidence for item in items)
        if not (confident or plain_yes(words) or plain_no(words)):
            return inbox(
                f"classified below {request.min_confidence:.2f} and not a plain yes or no"
            )
        return ModelWordsPromotion(
            verdict=EnumPromotionVerdict.RULING,
            rows=(_ruling_row(request, items, question),),
            question=question,
            reason="a decision answering a joined question",
        )


__all__ = [
    "HandlerWordsPromotion",
    "inbox_row",
    "plain_yes",
    "question_of",
    "scope_lines",
]
