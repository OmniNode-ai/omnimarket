# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parse supported code without executing it and record supplied test evidence."""

import ast
import json
import re

import yaml

from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.criteria_common import (
    added_diff_lines,
    fences,
    result,
    test_passes,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models import (
    EnumRubricOutcome,
    ModelCodeParsesParams,
    ModelIdsTraceableParams,
    ModelRubricCheckRequest,
    ModelRubricCriterion,
    ModelRubricCriterionResult,
)


def code_parses(
    request: ModelRubricCheckRequest, criterion: ModelRubricCriterion
) -> ModelRubricCriterionResult:
    params = criterion.params
    assert isinstance(params, ModelCodeParsesParams)
    if not request.answer_text.strip():
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "no_code")
    inferred_language = next(
        (
            language
            for language, phrases in (
                ("python", params.python_phrases),
                ("yaml", params.yaml_phrases),
                ("json", params.json_phrases),
            )
            if any(phrase.lower() in request.request_text.lower() for phrase in phrases)
        ),
        "",
    )
    blocks = fences(request.answer_text)
    if not blocks:
        try:
            value = json.loads(request.answer_text)
        except (ValueError, RecursionError):
            value = None
        if isinstance(value, dict):
            blocks = tuple(
                (inferred_language, value[name])
                for name in params.code_field_names
                if isinstance(value.get(name), str)
            )
        if not blocks and any(
            phrase.lower() in request.request_text.lower()
            for phrase in params.no_fence_phrases
        ):
            blocks = ((inferred_language, request.answer_text),)
    if not blocks:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "no_code")
    unsupported: list[str] = []
    parsed: list[str] = []
    for language, code in blocks:
        language = language or inferred_language
        try:
            if language in ("python", "py"):
                ast.parse(code)
            elif language in ("yaml", "yml"):
                yaml.safe_load(code)
            elif language == "json":
                json.loads(code)
            else:
                unsupported.append(language or "unlabelled")
                continue
        except SyntaxError as exc:
            line = str(exc.lineno)
            return result(
                criterion,
                EnumRubricOutcome.FAIL,
                "syntax_error",
                "line " + line,
                ("language:" + language, "line:" + line),
            )
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            line = str(mark.line + 1) if mark is not None else "unknown"
            return result(
                criterion,
                EnumRubricOutcome.FAIL,
                "syntax_error",
                "line " + line,
                ("language:" + language, "line:" + line),
            )
        except (ValueError, RecursionError) as exc:
            line = (
                str(exc.lineno) if isinstance(exc, json.JSONDecodeError) else "unknown"
            )
            return result(
                criterion,
                EnumRubricOutcome.FAIL,
                "syntax_error",
                "line " + line,
                ("language:" + language, "line:" + line),
            )
        parsed.append(language)
    if unsupported:
        return result(
            criterion,
            EnumRubricOutcome.UNDETERMINED,
            "language_unsupported",
            facts=tuple(unsupported),
        )
    return result(criterion, EnumRubricOutcome.PASS, "code_parsed", facts=tuple(parsed))


def stated_test_passes(
    request: ModelRubricCheckRequest, criterion: ModelRubricCriterion
) -> ModelRubricCriterionResult:
    return test_passes(request, criterion, include_request=True)


def ids_traceable(
    request: ModelRubricCheckRequest, criterion: ModelRubricCriterion
) -> ModelRubricCriterionResult:
    params = criterion.params
    assert isinstance(params, ModelIdsTraceableParams)
    scored = request.answer_text
    if params.diff_scope == "added_lines":
        added = added_diff_lines(request.answer_text)
        if added is not None:
            scored = added
    ids = tuple(dict.fromkeys(re.findall(params.ticket_id_pattern, scored)))
    if not ids:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "not_applicable")
    source_ids = set(
        re.findall(
            params.ticket_id_pattern, request.context + "\n" + request.request_text
        )
    )
    missing = tuple(value for value in ids if value not in source_ids)
    if missing:
        return result(
            criterion,
            EnumRubricOutcome.FAIL,
            "invented_id",
            ", ".join(missing),
            missing,
        )
    return result(criterion, EnumRubricOutcome.PASS, "ids_verified", facts=ids)
