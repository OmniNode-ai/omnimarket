# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure probe builder, leak audit and control contexts for compaction measurement.

HandlerCompactionProbes builds one probe per effective ruling from the row's own
``question=`` cell. The leak audit refuses a probe as ``PROBE_LEAK`` when it shares
five consecutive words with the ruling's quote (case and punctuation ignored),
before any arm runs. The handler also builds three control contexts:

* oracle: every effective ruling rendered verbatim, one line each, by stamp;
* empty: no text at all;
* inverted: for each ruling, its predecessor's text when it amended one, or else
  a negation of its quoted decision; every required span of every effective
  ruling is then redacted, so none appears verbatim.

HandlerProbeJudgeFixture is the deterministic, lexical stand-in for the judge: it
labels an answer ``CONTRADICTED`` when it restates the predecessor or states the
quote's content with the opposite polarity, ``RECALLED`` when it states the quote's
content with the same polarity or carries a required span, and ``ABSENT``
otherwise. The model judge of the measurement plan is calibrated separately.

No I/O, no clock and no randomness: equal inputs give equal outputs, whatever
order the rulings arrive in.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence

from omnimarket.nodes.node_compaction_probe_compute.models.model_compaction_probe import (
    EnumInversionSource,
    EnumProbeAudit,
    EnumProbeVerdict,
    ModelBuiltContext,
    ModelCompactionProbe,
    ModelCompactionProbeRequest,
    ModelCompactionProbeResult,
    ModelControlContexts,
    ModelEffectiveRuling,
    ModelInvertedEntry,
    ModelProbeJudgeRequest,
    ModelProbeJudgeResult,
)

__all__ = ["HandlerCompactionProbes", "HandlerProbeJudgeFixture"]

LEAK_WORDS = 5
PROBE_PREFIX = "Answer from this context alone, with no tools: "
INVERTED_PREFIX = "Ruling in force: "
NEGATION_PREFIX = "Refused; the operator decided the opposite of this: "
REDACTION_MARKER = "[redacted]"
_MARKER_ROUNDS = 8

_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_NEGATION = re.compile(r"(?i)\b(?:do not|don't|not|never|no)\b[ ,]*")
_NEGATORS = frozenset(
    ["no", "not", "never", "don't", "dont", "refused", "cannot", "can't", "won't"]
)


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _shingles(words: Sequence[str], size: int) -> list[tuple[str, ...]]:
    if size <= 0 or len(words) < size:
        return []
    return [tuple(words[i : i + size]) for i in range(len(words) - size + 1)]


def _first_shared_run(probe: str, quote: str) -> str | None:
    quote_runs = set(_shingles(_words(quote), LEAK_WORDS))
    for run in _shingles(_words(probe), LEAK_WORDS):
        if run in quote_runs:
            return " ".join(run)
    return None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _replace_all(text: str, spans: Sequence[str], marker: str) -> tuple[str, bool]:
    changed = False
    for span in spans:
        pattern = re.compile(re.escape(span), re.IGNORECASE)
        if pattern.search(text):
            replacement = "" if span.lower() in marker.lower() else marker
            text = pattern.sub(replacement, text)
            changed = True
    return text, changed


def redact_spans(text: str, spans: Iterable[str]) -> str:
    """Remove every span from ``text`` case-insensitively, to a fixed point.

    Spans become the marker while that converges; any span still present after
    that is deleted outright, which shrinks the text and so terminates.
    """
    ordered = sorted(set(spans), key=lambda span: (-len(span), span))
    for _ in range(_MARKER_ROUNDS):
        text, changed = _replace_all(text, ordered, REDACTION_MARKER)
        if not changed:
            return text
    changed = True
    while changed:
        text, changed = _replace_all(text, ordered, "")
    return text


def negate_quote(quote: str) -> str:
    """A deterministic negation of a quoted decision.

    A quote that carries a negation loses it (the affirmative of a refusal); any
    other quote is stated as refused.
    """
    if _NEGATION.search(quote):
        stripped = _NEGATION.sub("", quote).strip()
        return stripped[:1].upper() + stripped[1:]
    return f"{NEGATION_PREFIX}{quote}"


def _inverted_entry(
    ruling: ModelEffectiveRuling, spans: Sequence[str]
) -> ModelInvertedEntry:
    if ruling.predecessor_text is not None:
        source = EnumInversionSource.PREDECESSOR
        body = ruling.predecessor_text
    else:
        source = EnumInversionSource.NEGATION
        body = negate_quote(ruling.quote)
    return ModelInvertedEntry(
        stamp=ruling.stamp,
        source=source,
        text=redact_spans(f"{INVERTED_PREFIX}{body}", spans),
    )


def _context(name: str, text: str) -> ModelBuiltContext:
    return ModelBuiltContext(name=name, text=text, sha256=_sha256(text))


class HandlerCompactionProbes:
    """Build probes, refuse leaking ones, and build the three control contexts."""

    def handle(
        self, request: ModelCompactionProbeRequest
    ) -> ModelCompactionProbeResult:
        rulings = sorted(request.rulings, key=lambda ruling: ruling.stamp)
        probes: list[ModelCompactionProbe] = []
        for ruling in rulings:
            text = f"{PROBE_PREFIX}{ruling.question}"
            leaked = _first_shared_run(text, ruling.quote)
            probes.append(
                ModelCompactionProbe(
                    stamp=ruling.stamp,
                    kind=ruling.kind,
                    probe_text=text,
                    audit=EnumProbeAudit.ACCEPTED
                    if leaked is None
                    else EnumProbeAudit.PROBE_LEAK,
                    leaked_words=leaked,
                )
            )
        oracle = "\n".join(
            f'{r.stamp} | {r.kind} | question={r.question} | "{r.quote}"'
            for r in rulings
        )
        spans = [span for ruling in rulings for span in ruling.required_spans]
        entries = tuple(_inverted_entry(ruling, spans) for ruling in rulings)
        inverted = "\n".join(entry.text for entry in entries)
        refused = sum(1 for p in probes if p.audit is EnumProbeAudit.PROBE_LEAK)
        return ModelCompactionProbeResult(
            correlation_id=request.correlation_id,
            probes=tuple(probes),
            probes_total=len(probes),
            probes_accepted=len(probes) - refused,
            probes_refused_leak=refused,
            contexts=ModelControlContexts(
                oracle=_context("oracle", oracle),
                empty=_context("empty", ""),
                inverted=_context("inverted", inverted),
                inverted_entries=entries,
            ),
        )


def _content(text: str) -> list[str]:
    return [word for word in _words(text) if word not in _NEGATORS]


def _negative(text: str) -> bool:
    return any(word in _NEGATORS for word in _words(text))


def _overlap(answer: list[str], reference: list[str]) -> set[tuple[str, ...]]:
    size = min(LEAK_WORDS, len(reference))
    return set(_shingles(answer, size)) & set(_shingles(reference, size))


class HandlerProbeJudgeFixture:
    """Deterministic lexical judge fixture for one answer to a ruling's probe."""

    def handle(self, request: ModelProbeJudgeRequest) -> ModelProbeJudgeResult:
        ruling = request.ruling
        spans = ruling.required_spans

        def verdict(value: EnumProbeVerdict, reason: str) -> ModelProbeJudgeResult:
            return ModelProbeJudgeResult(
                stamp=ruling.stamp, verdict=value, reason=reason
            )

        if not request.answer.strip():
            return verdict(EnumProbeVerdict.ABSENT, "no answer")
        answer = redact_spans(request.answer, spans)
        quote = redact_spans(ruling.quote, spans)
        answer_words = _content(answer)
        quote_words = _content(quote)
        if ruling.predecessor_text is not None:
            predecessor = _content(redact_spans(ruling.predecessor_text, spans))
            shared = _overlap(answer_words, predecessor) - _overlap(
                quote_words, predecessor
            )
            if shared:
                return verdict(
                    EnumProbeVerdict.CONTRADICTED, "states the superseded ruling"
                )
        if _overlap(answer_words, quote_words):
            if _negative(answer) != _negative(quote):
                return verdict(
                    EnumProbeVerdict.CONTRADICTED,
                    "states the quoted decision with the opposite polarity",
                )
            return verdict(EnumProbeVerdict.RECALLED, "states the quoted decision")
        lowered = request.answer.lower()
        if any(span.lower() in lowered for span in spans):
            return verdict(EnumProbeVerdict.RECALLED, "cites a required span")
        return verdict(EnumProbeVerdict.ABSENT, "neither the decision nor its inverse")
