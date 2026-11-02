# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuse a drafted question that a ruling in force already answers.

The reply's questions are its sentences that end in a question mark or open with a
question lead (``Question:``, ``Ask:``, ``DECISION``). Quoted (``> ...``) and fenced
text is someone else's words and is not read. Each question is judged by the rule data
in order: a ``refuse`` rule whose patterns all match refuses it, an ``admit`` rule whose
option count is met admits it, and a question no rule matches is admitted. A rule acts
only while the request carries a RULING row with its stamp. One refused question refuses
the reply.

Pure over the request and the injected rule data: no file, environment or clock read.
"""

from __future__ import annotations

import re

from omnimarket.nodes.node_question_guard_compute.models.model_question_guard import (
    EnumQuestionGuardVerdict,
    ModelQuestionGuardRequest,
    ModelQuestionGuardResult,
    ModelQuestionGuardRule,
    ModelQuestionGuardRules,
    ModelQuestionVerdict,
)
from omnimarket.nodes.node_question_guard_compute.overlay import (
    load_question_guard_rules,
)

_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_RULING_TYPES = frozenset({"RULING", "OPERATOR-CONSENT"})
_SENTENCE_END = re.compile(r"(?<=[.?!])\s+")
_FENCE = "```"


def _rulings_in_force(rows: tuple[str, ...]) -> frozenset[str]:
    stamps: set[str] = set()
    for raw in rows:
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) >= 2 and _STAMP_RE.match(parts[0]) and parts[1] in _RULING_TYPES:
            stamps.add(parts[0])
    return frozenset(stamps)


def _paragraphs(text: str) -> list[str]:
    """The reply's own paragraphs, flattened; quoted and fenced lines are dropped."""
    paragraphs: list[str] = []
    current: list[str] = []
    fenced = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(_FENCE):
            fenced = not fenced
            continue
        if fenced or stripped.startswith(">"):
            continue
        if stripped:
            current.append(stripped)
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


def _questions(
    text: str, lead: re.Pattern[str], option_list: re.Pattern[str]
) -> list[str]:
    """Question sentences; an option list sentence right after a question joins it."""
    found: list[str] = []
    for paragraph in _paragraphs(text):
        previous_was_question = False
        for sentence in _SENTENCE_END.split(paragraph):
            sentence = sentence.strip()
            if not sentence:
                continue
            if previous_was_question and option_list.match(sentence):
                found[-1] = f"{found[-1]} {sentence}"
                continue
            previous_was_question = sentence.endswith("?") or bool(lead.match(sentence))
            if previous_was_question:
                found.append(sentence)
    return found


def _options(question: str, rules: ModelQuestionGuardRules) -> int:
    listed = re.search(rules.option_list, question, re.I)
    if listed:
        tail = question[listed.end() :]
        return len([o for o in tail.split(rules.option_list_separator) if o.strip()])
    alternatives = len(re.findall(rules.alternative, question, re.I))
    return alternatives + 1 if alternatives else 0


def _matches(rule: ModelQuestionGuardRule, question: str, options: int) -> bool:
    if not all(re.search(p, question, re.I) for p in rule.match_all):
        return False
    if rule.verdict is EnumQuestionGuardVerdict.ADMIT:
        return options >= rule.min_options
    return True


class HandlerQuestionGuard:
    """A typed verdict on a drafted reply: refuse with the matched ruling, or admit."""

    def __init__(self, rules: ModelQuestionGuardRules | None = None) -> None:
        self._rules = rules if rules is not None else load_question_guard_rules()

    def _judge(
        self, question: str, rules: ModelQuestionGuardRules, in_force: frozenset[str]
    ) -> ModelQuestionVerdict:
        options = _options(question, rules)
        for rule in rules.rules:
            if rule.ruling in in_force and _matches(rule, question, options):
                return ModelQuestionVerdict(
                    question=question,
                    options=options,
                    verdict=rule.verdict,
                    rule_id=rule.id,
                    ruling_id=rule.ruling,
                    reason=rule.reason,
                )
        return ModelQuestionVerdict(
            question=question,
            options=options,
            verdict=EnumQuestionGuardVerdict.ADMIT,
            reason=rules.unmatched_reason,
        )

    def handle(self, request: ModelQuestionGuardRequest) -> ModelQuestionGuardResult:
        rules = request.rules or self._rules
        in_force = _rulings_in_force(request.ruling_rows)
        lead = re.compile(rules.question_lead, re.I)
        option_list = re.compile(rules.option_list, re.I)
        verdicts = tuple(
            self._judge(q, rules, in_force)
            for q in _questions(request.reply_text, lead, option_list)
        )
        refused = [v for v in verdicts if v.verdict is EnumQuestionGuardVerdict.REFUSE]
        admitted = [v for v in verdicts if v.ruling_id]
        decisive = refused[0] if refused else (admitted[0] if admitted else None)
        if decisive is None:
            return ModelQuestionGuardResult(
                verdict=EnumQuestionGuardVerdict.ADMIT,
                reason=rules.unmatched_reason,
                questions=verdicts,
            )
        return ModelQuestionGuardResult(
            verdict=decisive.verdict,
            rule_id=decisive.rule_id,
            ruling_id=decisive.ruling_id,
            reason=decisive.reason,
            questions=verdicts,
        )


__all__ = ["HandlerQuestionGuard"]
