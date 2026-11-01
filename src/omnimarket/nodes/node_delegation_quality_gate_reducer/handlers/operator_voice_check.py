# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Operator-voice check: a drafted reply may not grant what the source never did.

A drafted reply that says "I approve" or "go ahead" speaks as the person it is
written for. When the source the draft was derived from holds no such grant, the
draft has invented a decision, and no amount of fluency makes it usable.

``evaluate_operator_voice`` is a pure function of the draft, the source and the
contract-declared policy. It calls no model, reads no file and holds no task
class: the contract arms it per class through the class's heuristic checks, and
the gate runs it for a class that lists the policy's check name.

A grant phrase in the draft is held against it unless one of these is true:

* the phrase is negated, deferred or asked about (``not approved``, ``whether to
  go ahead``, ``pending approval``), which is how a draft quotes a refusal or
  reports that no decision exists;
* the source holds a grant of the same group that is not itself negated, which
  is how a draft reports an approval the source contains.

What it does not claim: it reads phrases, so a grant worded outside the declared
patterns passes, and it does not attribute a grant to a speaker. A source that
holds a grant from anyone grounds a draft that reports one.
"""

from __future__ import annotations

import re
from functools import lru_cache

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_operator_voice import (
    ModelOperatorVoiceFinding,
    ModelOperatorVoicePolicy,
    ModelOperatorVoiceVerdict,
)


@lru_cache(maxsize=256)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.IGNORECASE)


def _without_spans(text: str, patterns: tuple[str, ...]) -> str:
    for pattern in patterns:
        text = re.sub(pattern, " ", text, flags=re.DOTALL | re.IGNORECASE)
    return text


def _is_negated(clause: str, start: int, policy: ModelOperatorVoicePolicy) -> bool:
    window = clause[max(0, start - policy.negation_window) : start]
    return any(
        _compiled(pattern).search(window) is not None
        for pattern in policy.negation_patterns
    )


def _grants(text: str, policy: ModelOperatorVoicePolicy) -> list[tuple[str, str, str]]:
    """``(group, phrase, clause)`` for every un-negated grant phrase in ``text``.

    Within a clause a phrase that overlaps an earlier one is dropped, so ``I
    approved`` is one grant, reported once under its most specific wording.
    """
    found: list[tuple[str, str, str]] = []
    for clause in _compiled(policy.clause_split).split(text):
        if not clause or not clause.strip():
            continue
        taken: list[tuple[int, int]] = []
        for group, patterns in policy.grant_groups.items():
            for pattern in patterns:
                for match in _compiled(pattern).finditer(clause):
                    span = (match.start(), match.end())
                    if any(span[0] < end and start < span[1] for start, end in taken):
                        continue
                    if _is_negated(clause, match.start(), policy):
                        continue
                    taken.append(span)
                    found.append((group, match.group(0), clause.strip()))
    return found


def evaluate_operator_voice(
    *,
    draft: str,
    source: str | None,
    policy: ModelOperatorVoicePolicy,
) -> ModelOperatorVoiceVerdict:
    """Find every grant the draft makes that the source does not hold.

    ``None`` -- a gate input with no source -- yields an unevaluated verdict,
    recorded by the caller as skipped, never as a pass.
    """
    if source is None:
        return ModelOperatorVoiceVerdict(evaluated=False)

    held = {group for group, _, _ in _grants(source, policy)}
    drafted = _grants(_without_spans(draft, policy.excluded_draft_spans), policy)
    seen: set[tuple[str, str]] = set()
    findings: list[ModelOperatorVoiceFinding] = []
    for group, phrase, clause in drafted:
        if group in held:
            continue
        key = (group, phrase.lower())
        if key in seen:
            continue
        seen.add(key)
        findings.append(
            ModelOperatorVoiceFinding(group=group, phrase=phrase, clause=clause[:200])
        )
    return ModelOperatorVoiceVerdict(
        evaluated=True,
        checked_count=len(drafted),
        findings=tuple(findings[: policy.failure_policy.max_reported]),
    )


@lru_cache(maxsize=1)
def resolve_operator_voice_policy() -> ModelOperatorVoicePolicy:
    """Load the gate contract's ``operator_voice`` block. Fail-closed."""
    return ModelOperatorVoicePolicy.model_validate(
        load_packaged_contract_section(
            "node_delegation_quality_gate_reducer", "operator_voice"
        )
    )


def operator_voice_check_name() -> str:
    """The contract-declared DoD name that arms the operator-voice check."""
    return resolve_operator_voice_policy().check_name


def check_operator_voice(
    content: str, grounding_source: str | None
) -> tuple[str | None, bool]:
    """Run the check for the gate: ``(failure_reason_or_None, evaluated)``.

    Each grant the draft makes that the source does not hold is named with its
    group and the phrase the draft used. Pure: no model is called.
    """
    verdict = evaluate_operator_voice(
        draft=content,
        source=grounding_source,
        policy=resolve_operator_voice_policy(),
    )
    if not verdict.evaluated:
        return None, False
    if not verdict.findings:
        return None, True
    rendered = ", ".join(finding.rendered() for finding in verdict.findings)
    return (
        f"OPERATOR_VOICE: {len(verdict.findings)} approval, consent or go-ahead "
        f"grant(s) in the draft occur nowhere in the source: {rendered}",
        True,
    )
