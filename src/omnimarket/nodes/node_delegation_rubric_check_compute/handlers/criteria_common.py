# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure text parsing shared across independently recorded criteria."""

import json
import re

from omnimarket.nodes.node_delegation_rubric_check_compute.models import (
    EnumRubricOutcome,
    ModelRubricCheckRequest,
    ModelRubricCriterion,
    ModelRubricCriterionResult,
    ModelTestTargetsParams,
)

CITED_LINES_PATTERN = (
    r"(?<![\w/])(?P<path>[A-Za-z_./][\w./-]*):(?P<start>\d+)(?:-(?P<end>\d+))?"
)


def result(
    criterion: ModelRubricCriterion,
    outcome: EnumRubricOutcome,
    reason: str,
    detail: str = "",
    facts: tuple[str, ...] = (),
) -> ModelRubricCriterionResult:
    return ModelRubricCriterionResult(
        criterion_id=criterion.criterion_id,
        outcome=outcome,
        reason_code=reason,
        detail=detail,
        facts=facts,
    )


def normalized(text: str) -> str:
    return " ".join(text.split())


def compact(text: str) -> str:
    """Whitespace-free form: a quote reflowed across lines still matches."""
    return "".join(text.split())


def fences(text: str) -> tuple[tuple[str, str], ...]:
    """Closed, line-delimited Markdown code fences, preserving first lines."""
    return tuple(
        (match.group("language").lower(), match.group("code"))
        for match in re.finditer(
            r"^[ \t]*```(?P<language>[\w+-]*)[^\S\n]*\n(?P<code>.*?)^[ \t]*```[ \t]*$",
            text,
            re.MULTILINE | re.DOTALL,
        )
    )


def json_text(text: str, allow_fences: bool) -> str:
    stripped = text.strip()
    blocks = fences(stripped)
    if allow_fences and len(blocks) == 1:
        match = re.fullmatch(r"```(?:json)?\s*\n.*\n```", stripped, re.DOTALL)
        if match:
            return blocks[0][1]
    return stripped


def parses_json(text: str) -> bool:
    try:
        json.loads(text)
    except (ValueError, RecursionError):
        return False
    return True


def test_passes(
    request: ModelRubricCheckRequest,
    criterion: ModelRubricCriterion,
    include_request: bool,
) -> ModelRubricCriterionResult:
    params = criterion.params
    assert isinstance(params, ModelTestTargetsParams)
    text = request.answer_text
    if include_request:
        text += "\n" + request.request_text
    targets = tuple(dict.fromkeys(re.findall(params.target_pattern, text)))
    if not targets:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "not_applicable")
    missing: list[str] = []
    failed: list[str] = []
    for target in targets:
        matches = [
            row
            for row in request.execution_results
            if row.target == target
            or (
                target.startswith("test_")
                and row.target.rsplit("::", 1)[-1].split("[", 1)[0] == target
            )
        ]
        if not matches:
            missing.append(target)
        elif any(not row.passed for row in matches):
            failed.append(target)
    if failed:
        return result(
            criterion,
            EnumRubricOutcome.FAIL,
            "test_failed",
            ", ".join(failed),
            tuple(failed),
        )
    if missing:
        return result(
            criterion,
            EnumRubricOutcome.UNDETERMINED,
            "no_execution_result",
            ", ".join(missing),
            tuple(missing),
        )
    return result(criterion, EnumRubricOutcome.PASS, "tests_verified", facts=targets)
