# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Only explicit prompt declarations activate format checks."""

import re

from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.criteria_common import (
    fences,
    json_text,
    parses_json,
    result,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models import (
    EnumRubricOutcome,
    ModelDeclaredFormatParams,
    ModelRubricCheckRequest,
    ModelRubricCriterion,
    ModelRubricCriterionResult,
)


def declared_format_met(
    request: ModelRubricCheckRequest, criterion: ModelRubricCriterion
) -> ModelRubricCriterionResult:
    params = criterion.params
    assert isinstance(params, ModelDeclaredFormatParams)
    prompt = request.request_text
    lower = prompt.lower()
    facts: list[str] = []
    if any(phrase.lower() in lower for phrase in params.json_phrases):
        allowed = any(phrase.lower() in lower for phrase in params.fence_allow_phrases)
        if not parses_json(json_text(request.answer_text, allowed)):
            return result(criterion, EnumRubricOutcome.FAIL, "not_json")
        facts.append("json_parsed")
    if any(phrase.lower() in lower for phrase in params.finding_phrases):
        declaration = next(
            (
                line
                for line in prompt.splitlines()
                if any(
                    phrase.lower() in line.lower() for phrase in params.finding_phrases
                )
            ),
            "",
        )
        severity = re.search(r"severity\(([^)]+)\)", declaration, re.IGNORECASE)
        if severity:
            # Severity alternatives contain pipes but occupy a single field.
            template = declaration.replace(severity[0], "severity")
            fields = template.split("|")
            severity_index = next(
                index
                for index, value in enumerate(fields)
                if "severity" in value.lower()
            )
            allowed_severities = tuple(
                value.strip() for value in severity[1].split("|")
            )
            for line in request.answer_text.splitlines():
                if not line.strip().startswith("FINDING"):
                    continue
                parts = [part.strip() for part in line.split("|")]
                if (
                    len(parts) != len(fields)
                    or parts[severity_index] not in allowed_severities
                    or re.search(r"`[^`]+`", parts[-1]) is None
                ):
                    return result(
                        criterion, EnumRubricOutcome.FAIL, "format_violation", line
                    )
                facts.append("finding_format_verified")
    blocks = fences(request.answer_text)
    one = re.search(params.one_fence_pattern, prompt, re.IGNORECASE)
    if one:
        language = one[1].lower()
        if len(blocks) != 1 or blocks[0][0] != language:
            return result(
                criterion,
                EnumRubricOutcome.FAIL,
                "format_violation",
                "expected one fenced " + language + " block",
            )
        facts.append("one_fence:" + language)
    first = re.search(params.first_line_pattern, prompt, re.IGNORECASE)
    if first:
        expected = first[1]
        # A declared placeholder such as <relative path> matches any non-empty text.
        shape = "".join(
            ".+" if part.startswith("<") else re.escape(part)
            for part in re.split(r"(<[^<>\n]+>)", expected)
            if part
        )
        if not blocks or any(
            not code.splitlines() or re.fullmatch(shape, code.splitlines()[0]) is None
            for _, code in blocks
        ):
            return result(
                criterion,
                EnumRubricOutcome.FAIL,
                "format_violation",
                "expected first line: " + expected,
            )
        facts.append("first_line_verified")
    if not facts:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "not_applicable")
    return result(
        criterion, EnumRubricOutcome.PASS, "format_verified", facts=tuple(facts)
    )
