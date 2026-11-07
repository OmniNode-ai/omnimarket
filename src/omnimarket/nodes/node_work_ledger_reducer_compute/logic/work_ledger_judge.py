# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reducer's judge: the ledger row grammar's guards, composed as one verdict (OMN-22915, L10).

The guards are the ledger-state checks the file judge runs, already ported as pure functions in
``omnimarket.handlers.ledger_row_grammar`` with their refusal rule names unchanged. This module only
composes them (row shape, then the checks that read the ledger text before the row) into a typed
verdict, so the shadow judge and the guard replay judge with one function and cannot drift apart.
"""

from __future__ import annotations

from omnimarket.handlers import ledger_row_grammar as grammar
from omnimarket.nodes.node_work_ledger_reducer_compute.models.model_work_ledger_judge import (
    EnumJudgeVerdict,
    ModelJudgeVerdict,
)

_RULE_SEPARATOR = " -- "


def rule_of(refusal: str) -> str:
    """The refusal's rule name: the text before the first separator, the reason's own words after."""
    return refusal.split(_RULE_SEPARATOR, 1)[0]


def judge_row(row: str, existing: str, ledger_name: str) -> ModelJudgeVerdict:
    """Accept or refuse ``row`` appended to the ledger text ``existing``; pure."""
    refusal = grammar.refusal_for_payload(
        row, ledger_name
    ) or grammar.refusal_for_state(row, existing, ledger_name)
    if refusal is None:
        return ModelJudgeVerdict(verdict=EnumJudgeVerdict.ACCEPT)
    return ModelJudgeVerdict(
        verdict=EnumJudgeVerdict.REFUSE, rule=rule_of(refusal), reason=refusal
    )


def verdict_difference(
    first: ModelJudgeVerdict, second: ModelJudgeVerdict, *, names: tuple[str, str]
) -> str | None:
    """How two verdicts differ, naming each side, or None when they are the same verdict and rule."""
    a_name, b_name = names
    if first.verdict is not second.verdict:
        accepted, refused, refusing = (
            (a_name, b_name, second)
            if first.verdict is EnumJudgeVerdict.ACCEPT
            else (b_name, a_name, first)
        )
        return f"{accepted} accepted; {refused} refused under {refusing.rule}"
    if first.verdict is EnumJudgeVerdict.REFUSE and first.rule != second.rule:
        return f"both refused; {a_name} rule {first.rule}, {b_name} rule {second.rule}"
    return None


__all__: list[str] = ["judge_row", "rule_of", "verdict_difference"]
