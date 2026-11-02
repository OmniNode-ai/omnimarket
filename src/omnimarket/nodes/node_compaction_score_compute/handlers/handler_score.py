# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure scoring of a compacted context from recorded answers and the pinned ledger prefix.

- S1: per decision item and repeat, one verdict: ``RECALLED``, ``ABSENT``,
  ``CONTRADICTED`` or ``UNAVAILABLE``. A lexical reading (the answer cites the stamp or
  five consecutive words of the quote) and a judged reading are both kept; the judge's
  verdict decides when it is recorded. ``ABSENT`` and ``CONTRADICTED`` are separate counts.
  Planted items form their own stratum and are never pooled with ledger items.
- S2, mention reading: an open-loop item is mentioned when a name of it appears in the
  compacted context or in an available "what is open" answer.
- S4, deterministic families: an identifier in the context that the ledger prefix does not
  hold is a false memory; a closed lane stated open, a superseded ruling stated in force,
  or an amended ruling quoted as complete without its amender is a stale memory.

Per key the latest valid attempt counts; no valid attempt is ``UNAVAILABLE``, which stays in
the denominator and can never establish a pass. The fold never reads the request's arm label.
"""

from __future__ import annotations

import math
import re

from omnimarket.nodes.node_compaction_score_compute.models.model_score import (
    DECISION_KINDS,
    OPEN_LOOP_KINDS,
    EnumControl,
    EnumStratum,
    EnumVerdict,
    ModelControlCheck,
    ModelGroundTruthItem,
    ModelLedgerPrefix,
    ModelMentionScore,
    ModelPrecisionScore,
    ModelRecallScore,
    ModelRecordedAnswer,
    ModelScoreRequest,
    ModelScoreResult,
    ModelStaleMemory,
    ModelVerdictCounts,
)

__all__ = ["HandlerCompactionScore", "check_control", "wilson_lower"]

QUOTE_WINDOW = 5
ORACLE_RECALL_FLOOR = 0.95
ORACLE_WILSON_FLOOR = 0.90
EMPTY_CEILING = 0.05
INVERTED_FLOOR = 0.90

_WORDS = re.compile(r"\w+")
_TICKET = re.compile(r"\bOMN-\d+\b")
_PULL_REQUEST = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#\d+")
_TIMESTAMP = re.compile(r"\b\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\b")
_SHA = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")
_PATH = re.compile(
    r"(?<![\w/.-])(?:[\w.-]+/)+[\w.-]+\.(?:py|yaml|yml|md|json|toml|sh|txt)\b"
)
_LANE = re.compile(r"\blane=([\w-]+)")
_STATEMENT_BREAK = re.compile(r"(?<=[.!?])\s+|\n")
_OPEN_WORDS = re.compile(
    r"\b(open|running|in flight|active|live|pending|in progress|still)\b", re.IGNORECASE
)
_CLOSED_WORDS = re.compile(
    r"\b(closed|done|terminal|merged|finished|complete|completed|released|ended|stopped)\b",
    re.IGNORECASE,
)
_FORCE_WORDS = re.compile(
    r"\b(in force|active|stands|effective|binding|current)\b", re.IGNORECASE
)
_RETIRED_WORDS = re.compile(
    r"\b(superseded|replaced|closed|withdrawn|amended|no longer)\b", re.IGNORECASE
)
_COMPLETE_WORDS = re.compile(
    r"\b(complete|in full|entire|final|whole|only rule)\b", re.IGNORECASE
)


def wilson_lower(successes: int, trials: int, z: float = 1.96) -> float | None:
    """Lower bound of the Wilson score interval; None when there are no trials."""
    if trials == 0:
        return None
    p = successes / trials
    centre = p + z * z / (2 * trials)
    spread = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials))
    return (centre - spread) / (1 + z * z / trials)


def _words(text: str) -> list[str]:
    return [word.lower() for word in _WORDS.findall(text)]


def _lexical_hit(item: ModelGroundTruthItem, text: str) -> bool:
    """The answer cites the ruling by stamp, or by five consecutive words of the quote."""
    if item.key.lower() in text.lower():
        return True
    quote = _words(item.quote)
    answer = _words(text)
    size = min(QUOTE_WINDOW, len(quote))
    if size == 0:
        return False
    windows = {tuple(quote[i : i + size]) for i in range(len(quote) - size + 1)}
    return any(
        tuple(answer[i : i + size]) in windows for i in range(len(answer) - size + 1)
    )


def _latest_valid(
    answers: tuple[ModelRecordedAnswer, ...], key: str, repeat: int
) -> ModelRecordedAnswer | None:
    valid = [
        a for a in answers if a.item_key == key and a.repeat == repeat and a.available
    ]
    return max(valid, key=lambda a: a.attempt) if valid else None


def _verdict(
    item: ModelGroundTruthItem, answer: ModelRecordedAnswer | None
) -> EnumVerdict:
    if answer is None:
        return EnumVerdict.UNAVAILABLE
    if answer.judged is not None:
        return answer.judged
    if answer.text.strip() and _lexical_hit(item, answer.text):
        return EnumVerdict.RECALLED
    return EnumVerdict.ABSENT


def _recall(request: ModelScoreRequest, stratum: EnumStratum) -> ModelRecallScore:
    items = [
        i for i in request.items if i.kind in DECISION_KINDS and i.stratum is stratum
    ]
    verdicts: dict[str, tuple[EnumVerdict, ...]] = {}
    lexical = 0
    for item in items:
        row: list[EnumVerdict] = []
        for repeat in range(request.repeats):
            answer = _latest_valid(request.answers, item.key, repeat)
            row.append(_verdict(item, answer))
            if (
                answer is not None
                and answer.text.strip()
                and _lexical_hit(item, answer.text)
            ):
                lexical += 1
        verdicts[item.key] = tuple(row)
    flat = [v for row in verdicts.values() for v in row]
    counts = ModelVerdictCounts(
        recalled=flat.count(EnumVerdict.RECALLED),
        absent=flat.count(EnumVerdict.ABSENT),
        contradicted=flat.count(EnumVerdict.CONTRADICTED),
        unavailable=flat.count(EnumVerdict.UNAVAILABLE),
    )
    trials = len(flat)
    return ModelRecallScore(
        stratum=stratum,
        items=len(items),
        trials=trials,
        judged=counts,
        lexical_recalled=lexical,
        recall=counts.recalled / trials if trials else None,
        recall_wilson_lower=wilson_lower(counts.recalled, trials),
        contradiction=counts.contradicted / trials if trials else None,
        lexical_recall=lexical / trials if trials else None,
        item_verdicts=verdicts,
    )


def _name_pattern(names: tuple[str, ...]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(name) for name in names)
    return re.compile(rf"(?<![\w-])(?:{alternatives})(?![\w-])", re.IGNORECASE)


def _mention(request: ModelScoreRequest) -> ModelMentionScore:
    texts = [request.context_text] + [
        a.text for a in request.open_probe_answers if a.available
    ]
    items = [
        i
        for i in request.items
        if i.kind in OPEN_LOOP_KINDS and i.stratum is EnumStratum.LEDGER
    ]
    missed = tuple(
        i.key
        for i in items
        if not any(_name_pattern(i.mention_names).search(t) for t in texts)
    )
    mentioned = len(items) - len(missed)
    return ModelMentionScore(
        items=len(items),
        mentioned=mentioned,
        mention=mentioned / len(items) if items else None,
        unmentioned=missed,
    )


def _identifiers(text: str) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    for family, pattern in (
        ("ticket", _TICKET),
        ("pull_request", _PULL_REQUEST),
        ("timestamp", _TIMESTAMP),
        ("sha", _SHA),
        ("path", _PATH),
    ):
        found.extend((family, m.group(0)) for m in pattern.finditer(text))
    found.extend(("lane", m.group(1)) for m in _LANE.finditer(text))
    return list(dict.fromkeys(found))


def _known(prefix: ModelLedgerPrefix, family: str, value: str) -> bool:
    if family == "sha":
        return any(sha.startswith(value) for sha in prefix.shas)
    pool = {
        "ticket": prefix.tickets,
        "pull_request": prefix.pull_requests,
        "timestamp": prefix.timestamps,
        "path": prefix.paths,
        "lane": prefix.lanes,
    }[family]
    return value in pool


def _mentions(statement: str, name: str) -> bool:
    return _name_pattern((name,)).search(statement) is not None


def _stale(prefix: ModelLedgerPrefix, text: str) -> list[ModelStaleMemory]:
    stale: list[ModelStaleMemory] = []
    for statement in _STATEMENT_BREAK.split(text):
        for lane in prefix.closed_lanes:
            if not _mentions(statement, lane):
                continue
            bare = _name_pattern((lane,)).sub(" ", statement)
            if _OPEN_WORDS.search(bare) and not _CLOSED_WORDS.search(bare):
                stale.append(
                    ModelStaleMemory(
                        reason="lane_closed", identifier=lane, line=statement
                    )
                )
        for stamp in prefix.superseded_rulings:
            if (
                _mentions(statement, stamp)
                and _FORCE_WORDS.search(statement)
                and not _RETIRED_WORDS.search(statement)
            ):
                stale.append(
                    ModelStaleMemory(
                        reason="ruling_superseded", identifier=stamp, line=statement
                    )
                )
        for target, amenders in prefix.amended_rulings.items():
            if (
                _mentions(statement, target)
                and _COMPLETE_WORDS.search(statement)
                and not any(_mentions(statement, amender) for amender in amenders)
            ):
                stale.append(
                    ModelStaleMemory(
                        reason="amender_missing", identifier=target, line=statement
                    )
                )
    return stale


def _precision(request: ModelScoreRequest) -> ModelPrecisionScore:
    identifiers = _identifiers(request.context_text)
    return ModelPrecisionScore(
        false_memories=tuple(
            value
            for family, value in identifiers
            if not _known(request.ledger, family, value)
        ),
        stale_memories=tuple(_stale(request.ledger, request.context_text)),
        identifiers_checked=len(identifiers),
    )


class HandlerCompactionScore:
    def handle(self, request: ModelScoreRequest) -> ModelScoreResult:
        return ModelScoreResult(
            case_id=request.case_id,
            manifest_digest=request.manifest_digest,
            ledger_recall=_recall(request, EnumStratum.LEDGER),
            planted_recall=_recall(request, EnumStratum.PLANTED),
            mention=_mention(request),
            precision=_precision(request),
        )


def check_control(control: EnumControl, result: ModelScoreResult) -> ModelControlCheck:
    """Controls 2 to 4 of the plan, read from a fold the caller ran over that control's arm."""
    recall = result.ledger_recall
    failures: list[str] = []
    if recall.trials == 0:
        failures.append("no ledger trials to judge a control on")
    elif control is EnumControl.ORACLE:
        if (recall.recall or 0.0) < ORACLE_RECALL_FLOOR:
            failures.append(f"recall {recall.recall} below {ORACLE_RECALL_FLOOR}")
        if (recall.recall_wilson_lower or 0.0) < ORACLE_WILSON_FLOOR:
            failures.append(
                f"recall wilson lower bound {recall.recall_wilson_lower} below {ORACLE_WILSON_FLOOR}"
            )
        if recall.judged.contradicted:
            failures.append(f"contradiction {recall.contradiction} is not 0")
    elif control is EnumControl.EMPTY:
        if (recall.recall or 0.0) > EMPTY_CEILING:
            failures.append(f"recall {recall.recall} above {EMPTY_CEILING}")
        if (result.mention.mention or 0.0) > EMPTY_CEILING:
            failures.append(f"mention {result.mention.mention} above {EMPTY_CEILING}")
    elif (recall.contradiction or 0.0) < INVERTED_FLOOR:
        failures.append(f"contradiction {recall.contradiction} below {INVERTED_FLOOR}")
    return ModelControlCheck(
        control=control, passed=not failures, failures=tuple(failures)
    )
