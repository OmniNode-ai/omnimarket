# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Source traceability of extractable claims and requested ticket coverage."""

import re

from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.criteria_common import (
    normalized,
    result,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models import (
    EnumRubricOutcome,
    ModelClaimsTraceableParams,
    ModelIdCoverageParams,
    ModelRubricCheckRequest,
    ModelRubricCriterion,
    ModelRubricCriterionResult,
)


def _numbers(text: str, min_digits: int, ticket_pattern: str) -> tuple[str, ...]:
    # Digits within structured references are part of that reference, not
    # independent numerical claims.
    text = re.sub(ticket_pattern, " ", text)
    text = re.sub(r"(?:\b[\w.-]+(?:/[\w.-]+)?)?#\d+\b", " ", text)
    numbers = (
        re.sub(r"[,_]", "", match[0])
        for match in re.finditer(r"(?<!\w)\d+(?:[,_]\d+)*(?:\.\d+)?(?!\w)", text)
    )
    return tuple(
        value
        for value in numbers
        if sum(char.isdigit() for char in value) >= min_digits
    )


def _missing(
    unit: str, source: str, params: ModelClaimsTraceableParams
) -> tuple[str, ...]:
    ticket_ids = re.findall(params.ticket_id_pattern, unit)
    source_ids = set(re.findall(params.ticket_id_pattern, source))
    pr_pattern = r"\b[\w.-]+(?:/[\w.-]+)?#\d+\b"
    pr_refs = re.findall(pr_pattern, unit)
    source_refs = set(re.findall(pr_pattern, source))
    structured_missing = [value for value in ticket_ids if value not in source_ids]
    # A ref is traceable when the source carries its number as a ref, qualified
    # or bare: "repo#1 and #2" names repo#2 as well.
    source_ref_numbers = {ref.rsplit("#", 1)[1] for ref in source_refs} | set(
        re.findall(r"(?<![\w#])#(\d+)\b", source)
    )
    structured_missing.extend(
        value
        for value in pr_refs
        if value not in source_refs
        and value.rsplit("#", 1)[1] not in source_ref_numbers
    )
    structured_missing.extend(
        "#" + number
        for number in re.findall(r"(?<![\w#])#(\d+)\b", unit)
        if number not in source_ref_numbers
    )
    tokens: list[str] = []
    tokens.extend(
        match[0].rstrip(".")
        for match in re.finditer(
            r"(?<![\w/])(?:[\w.-]+/)*[\w.-]+\.[A-Za-z][\w-]*(?!\w)", unit
        )
    )
    tokens.extend(re.findall(r"`([^`\n]+)`", unit))
    tokens.extend(
        quote
        for quote in re.findall(r'"([^"\n]+)"', unit)
        if len(quote.split()) >= params.min_quote_words
    )
    source_normalized = normalized(source)
    missing = structured_missing + [
        token for token in tokens if normalized(token) not in source_normalized
    ]
    source_numbers = set(
        _numbers(source, params.min_number_digits, params.ticket_id_pattern)
    )
    missing.extend(
        number
        for number in _numbers(unit, params.min_number_digits, params.ticket_id_pattern)
        if number not in source_numbers
    )
    return tuple(dict.fromkeys(missing))


def _has_items(unit: str, params: ModelClaimsTraceableParams) -> bool:
    # Extraction against an empty source names every extractable item.
    return bool(_missing(unit, "", params))


def claims_traceable(
    request: ModelRubricCheckRequest, criterion: ModelRubricCriterion
) -> ModelRubricCriterionResult:
    params = criterion.params
    assert isinstance(params, ModelClaimsTraceableParams)
    # A list marker ("12." or "3)") numbers the answer's own items; it is not a claim.
    units = tuple(
        unit.strip()
        for line in request.answer_text.splitlines()
        for unit in re.split(
            r"(?<=[.!?])\s+", re.sub(r"^\s*(?:\d+[.)]|[-*+])\s+", "", line)
        )
        if unit.strip()
    )
    failures: list[tuple[str, tuple[str, ...]]] = []
    extractable = False
    for unit in units:
        extractable = extractable or _has_items(unit, params)
        missing = _missing(unit, request.context, params)
        if missing:
            failures.append((unit, missing))
    if failures:
        first, missing = failures[0]
        all_missing = tuple(
            dict.fromkeys(token for _, tokens in failures for token in tokens)
        )
        return result(
            criterion,
            EnumRubricOutcome.FAIL,
            "sentence_untraceable",
            first + " Missing: " + ", ".join(missing),
            all_missing[: params.max_facts],
        )
    if not extractable:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "no_extractable_items")
    return result(criterion, EnumRubricOutcome.PASS, "claims_verified")


def id_coverage(
    request: ModelRubricCheckRequest, criterion: ModelRubricCriterion
) -> ModelRubricCriterionResult:
    params = criterion.params
    assert isinstance(params, ModelIdCoverageParams)
    if not any(
        re.search(pattern, request.request_text, re.IGNORECASE)
        for pattern in params.trigger_patterns
    ):
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "not_applicable")
    ids = tuple(dict.fromkeys(re.findall(params.ticket_id_pattern, request.context)))
    if not ids:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "not_applicable")
    answer_ids = set(re.findall(params.ticket_id_pattern, request.answer_text))
    missing = tuple(value for value in ids if value not in answer_ids)
    if missing:
        return result(
            criterion,
            EnumRubricOutcome.FAIL,
            "id_missing_from_answer",
            ", ".join(missing),
            missing,
        )
    return result(criterion, EnumRubricOutcome.PASS, "ids_covered", facts=ids)
