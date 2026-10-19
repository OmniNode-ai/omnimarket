# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure compaction loss measurement (OMN-21122).

HandlerCompactionPairs splits a Claude Code transcript at each ``compact_boundary``
record. The window a compaction replaced is every record after the previous boundary
and before this one; a window opens with the previous compaction's summary, which is
carried context. The replacement is the ``isCompactSummary`` user record after the
boundary, or, when none is marked, the first user record after it. The compaction id
is the boundary record's ``uuid``, the key the ``compact`` capture kind journals.
Claude Code appends to the transcript and never rewrites it, so the PreCompact view
of a window is recoverable from the same ``transcript_path`` after PostCompact.

The window's text is the conversation's own words: user and assistant text blocks
plus tool_use inputs (where a session writes its CLAIM rows). Tool results, thinking
blocks, meta records and local command wrappers are left out.

HandlerCompactionLoss extracts must-carry items from the window and checks each
against the summary, case-insensitively. Identifier kinds (ticket, pr, lane_claim,
ruling, operator_mode) carry one span, the identifier; prose kinds (promise,
operator_instruction) carry the stems (first six letters) of the up-to-three longest
content words of their sentence, filler words excluded, so a restated inflection still
matches; an item survives only when every span is in the summary. Extraction is deterministic
pattern matching, so it measures what the patterns see, not every obligation. The
result carries an eval case with the replay eval presence-case field names, so a
context pack is judged on the same items by replacing ``package_text``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from pydantic import JsonValue

from omnimarket.nodes.node_compaction_loss_compute.models import (
    EnumMustCarryKind,
    ModelCompactionLossRequest,
    ModelCompactionLossResult,
    ModelCompactionPairs,
    ModelCompactionPairsRequest,
    ModelKindRecall,
    ModelLossEvalCase,
    ModelLossExpected,
    ModelMustCarryItem,
)

__all__ = ["HandlerCompactionLoss", "HandlerCompactionPairs"]

_TICKET = re.compile(r"\bOMN-\d+\b")
_PR = re.compile(r"\b([A-Za-z][A-Za-z0-9_.-]*#\d+)\b")
_CLAIM = re.compile(r"\|\s*CLAIM\s*\|[^\n']*?\blane=([A-Za-z0-9._-]+)")
_TIMESTAMP = re.compile(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\b")
_RULING_WORD = re.compile(r"(?i)ruling|consent")
_SILENT = re.compile(r"(?i)\bsilent\b")
_PROMISE = re.compile(
    r"(?i)\b(i will|i'll|i am going to|i'm going to|i promise|i owe)\b"
)
_INSTRUCTION = re.compile(
    r"(?i)\b(never|always|don't|do not|make sure|keep|must|stop|only)\b"
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
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
_CANDIDATE = "stock-summary"


def _strings(value: JsonValue) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _is_command_wrapper(text: str) -> bool:
    return text.lstrip().startswith(("<command-", "<local-command"))


def _record_parts(record: dict[str, JsonValue]) -> tuple[str, list[str], list[str]]:
    """Return (role, spoken text parts, tool-input parts) of one transcript record."""
    role = record.get("type")
    if role not in ("user", "assistant") or record.get("isMeta") is True:
        return "", [], []
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    spoken: list[str] = []
    tool_inputs: list[str] = []
    if isinstance(content, str):
        spoken.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            text = block.get("text")
            if kind == "text" and isinstance(text, str):
                spoken.append(text)
            elif kind == "tool_use" and role == "assistant":
                tool_inputs.extend(_strings(block.get("input")))
    spoken = [part for part in spoken if not _is_command_wrapper(part)]
    if role == "user" and record.get("isCompactSummary") is True:
        return "carried_summary", spoken, []
    return str(role), spoken, tool_inputs


def _summary_text(records: list[dict[str, JsonValue]]) -> str:
    marked = [r for r in records if r.get("isCompactSummary") is True]
    for record in marked or [r for r in records if r.get("type") == "user"]:
        _, spoken, _ = _record_parts({**record, "isCompactSummary": False})
        if spoken:
            return "\n".join(spoken)
    return ""


def _int(value: JsonValue) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class HandlerCompactionPairs:
    """Split a transcript into one loss request per compact_boundary record."""

    def handle(self, request: ModelCompactionPairsRequest) -> ModelCompactionPairs:
        records = list(request.records)
        boundaries = [
            index
            for index, record in enumerate(records)
            if record.get("type") == "system"
            and record.get("subtype") == "compact_boundary"
        ]
        pairs: list[ModelCompactionLossRequest] = []
        for position, index in enumerate(boundaries):
            start = boundaries[position - 1] + 1 if position else 0
            end = (
                boundaries[position + 1]
                if position + 1 < len(boundaries)
                else len(records)
            )
            text: list[str] = []
            user: list[str] = []
            assistant: list[str] = []
            for record in records[start:index]:
                role, spoken, tool_inputs = _record_parts(record)
                text.extend(spoken)
                text.extend(tool_inputs)
                if role == "user":
                    user.extend(spoken)
                elif role == "assistant":
                    assistant.extend(spoken)
            boundary = records[index]
            metadata = boundary.get("compactMetadata")
            meta = metadata if isinstance(metadata, dict) else {}
            trigger = meta.get("trigger")
            pairs.append(
                ModelCompactionLossRequest(
                    session_id=request.session_id,
                    compaction_id=str(boundary.get("uuid") or f"boundary-{index}"),
                    trigger=trigger if isinstance(trigger, str) else None,
                    transcript_path=request.transcript_path,
                    pre_tokens=_int(meta.get("preTokens")),
                    post_tokens=_int(meta.get("postTokens")),
                    pre_text="\n".join(text),
                    pre_user_text="\n".join(user),
                    pre_assistant_text="\n".join(assistant),
                    post_summary=_summary_text(records[index + 1 : end]),
                )
            )
        return ModelCompactionPairs(session_id=request.session_id, pairs=tuple(pairs))


def _sentences(text: str, pattern: re.Pattern[str]) -> Iterator[str]:
    for sentence in _SENTENCE_SPLIT.split(text):
        sentence = sentence.strip()
        if sentence and pattern.search(sentence):
            yield sentence


def _content_spans(sentence: str) -> tuple[str, ...]:
    words = {w for w in _WORD.findall(sentence.lower()) if w not in _STOPWORDS}
    longest = sorted(words, key=lambda w: (-len(w), w))
    stems: list[str] = []
    for word in longest:
        stem = word[:_STEM]
        if stem not in stems:
            stems.append(stem)
    return tuple(stems[:3])


class HandlerCompactionLoss:
    """Score which must-carry items of a compacted window survive in its summary."""

    def handle(self, request: ModelCompactionLossRequest) -> ModelCompactionLossResult:
        summary = request.post_summary.lower()
        items: dict[tuple[EnumMustCarryKind, str], ModelMustCarryItem] = {}

        def add(
            kind: EnumMustCarryKind, key: str, text: str, spans: tuple[str, ...]
        ) -> None:
            if not spans or (kind, key) in items:
                return
            survived = all(span.lower() in summary for span in spans)
            items[(kind, key)] = ModelMustCarryItem(
                kind=kind, key=key, text=text, required_spans=spans, survived=survived
            )

        pre = request.pre_text
        for match in _CLAIM.finditer(pre):
            add(
                EnumMustCarryKind.LANE_CLAIM,
                match.group(1),
                match.group(0),
                (match.group(1),),
            )
        for match in _TIMESTAMP.finditer(pre):
            context = pre[max(0, match.start() - 40) : match.end() + 30]
            if _RULING_WORD.search(context):
                ts = match.group(0)
                add(EnumMustCarryKind.RULING, ts, context.strip(), (ts,))
        for match in _PR.finditer(pre):
            add(EnumMustCarryKind.PR, match.group(1), match.group(1), (match.group(1),))
        for match in _TICKET.finditer(pre):
            add(
                EnumMustCarryKind.TICKET,
                match.group(0),
                match.group(0),
                (match.group(0),),
            )
        if _SILENT.search(request.pre_user_text) or _SILENT.search(pre):
            add(EnumMustCarryKind.OPERATOR_MODE, "silent", "silent mode", ("silent",))
        for sentence in _sentences(request.pre_assistant_text, _PROMISE):
            add(
                EnumMustCarryKind.PROMISE,
                sentence[:120],
                sentence,
                _content_spans(sentence),
            )
        for sentence in _sentences(request.pre_user_text, _INSTRUCTION):
            add(
                EnumMustCarryKind.OPERATOR_INSTRUCTION,
                sentence[:120],
                sentence,
                _content_spans(sentence),
            )

        ordered = tuple(items.values())
        recall: list[ModelKindRecall] = []
        for kind in EnumMustCarryKind:
            of_kind = [item for item in ordered if item.kind is kind]
            if of_kind:
                kept = sum(1 for item in of_kind if item.survived)
                recall.append(
                    ModelKindRecall(
                        kind=kind,
                        total=len(of_kind),
                        survived=kept,
                        recall=kept / len(of_kind),
                    )
                )
        survived = sum(1 for item in ordered if item.survived)
        return ModelCompactionLossResult(
            session_id=request.session_id,
            compaction_id=request.compaction_id,
            trigger=request.trigger,
            transcript_path=request.transcript_path,
            pre_tokens=request.pre_tokens,
            post_tokens=request.post_tokens,
            items=ordered,
            items_total=len(ordered),
            items_survived=survived,
            overall_recall=survived / len(ordered) if ordered else None,
            recall_by_kind=tuple(recall),
            dropped=tuple(item for item in ordered if not item.survived),
            eval_case=ModelLossEvalCase(
                case_id=f"{request.session_id}:{request.compaction_id}",
                candidate_id=_CANDIDATE,
                package_text=request.post_summary,
                expected=tuple(
                    ModelLossExpected(
                        statement_id=f"{item.kind.value}:{item.key}",
                        required_spans=item.required_spans,
                    )
                    for item in ordered
                ),
            ),
        )
