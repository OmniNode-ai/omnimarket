# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Only explicit prompt declarations activate format checks."""

import json
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


def _declared_facts(prompt: str, pattern: str) -> dict[str, object] | None:
    """The JSON object of facts the prompt supplies, or None when unreadable."""
    start = re.search(pattern, prompt)
    if start is None:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(prompt, start.end())
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def declared_format_met(
    request: ModelRubricCheckRequest, criterion: ModelRubricCriterion
) -> ModelRubricCriterionResult:
    params = criterion.params
    assert isinstance(params, ModelDeclaredFormatParams)
    prompt = request.request_text
    lower = prompt.lower()
    facts: list[str] = []
    single = re.search(params.single_word_pattern, prompt, re.IGNORECASE)
    if single:
        word = single[1]
        if request.answer_text.strip() != word:
            return result(
                criterion,
                EnumRubricOutcome.FAIL,
                "format_violation",
                "expected the single word: " + word,
            )
        facts.append("single_word_verified")
    if any(phrase.lower() in lower for phrase in params.json_phrases):
        allowed = any(phrase.lower() in lower for phrase in params.fence_allow_phrases)
        text = json_text(request.answer_text, allowed)
        if not parses_json(text):
            return result(criterion, EnumRubricOutcome.FAIL, "not_json")
        facts.append("json_parsed")
        value = json.loads(text)
        for choice in re.finditer(params.json_choice_pattern, prompt):
            field = choice[1]
            alternatives = re.findall(r'"([^"]+)"', choice[2])
            if not isinstance(value, dict) or value.get(field) not in alternatives:
                return result(
                    criterion,
                    EnumRubricOutcome.FAIL,
                    "format_violation",
                    field + " not one of " + ", ".join(alternatives),
                )
            facts.append("choice_verified:" + field)
        for echo in re.finditer(params.echo_field_pattern, prompt):
            field = echo[1]
            source_facts = _declared_facts(prompt, params.facts_object_pattern)
            if source_facts is None:
                # An echo that cannot be checked against the facts never passes.
                return result(
                    criterion, EnumRubricOutcome.UNDETERMINED, "facts_unreadable"
                )
            echoed = value.get(field) if isinstance(value, dict) else None
            if not isinstance(echoed, dict) or not echoed:
                return result(
                    criterion,
                    EnumRubricOutcome.FAIL,
                    "format_violation",
                    field + " missing",
                )
            for key, echoed_value in echoed.items():
                if (
                    key not in source_facts
                    or type(echoed_value) is not type(source_facts[key])
                    or echoed_value != source_facts[key]
                ):
                    return result(
                        criterion,
                        EnumRubricOutcome.FAIL,
                        "format_violation",
                        field + ": " + key,
                    )
            facts.append("facts_echo_verified:" + field)
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
