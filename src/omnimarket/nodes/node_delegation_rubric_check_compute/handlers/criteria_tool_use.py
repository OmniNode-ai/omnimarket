# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure checks over caller-recorded tool calls and workspace evidence."""

import json
import re

from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.criteria_common import (
    CITED_LINES_PATTERN,
    result,
    test_passes,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.criteria_summarization import (
    _missing,
    answer_units,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models import (
    EnumRubricOutcome,
    EnumToolCallStatus,
    EnumToolParameterType,
    ModelClaimsTraceableParams,
    ModelEditsApplyParams,
    ModelNoPhantomPathsParams,
    ModelRubricCheckRequest,
    ModelRubricCriterion,
    ModelRubricCriterionResult,
    ModelToolCallsWellformedParams,
    ModelWithinBudgetParams,
)


def _arguments(text: str) -> dict[str, object] | None:
    try:
        value: object = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict):
        return None
    return {str(key): item for key, item in value.items()}


def _matches_type(value: object, expected: EnumToolParameterType) -> bool:
    if expected == EnumToolParameterType.INTEGER:
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == EnumToolParameterType.NUMBER:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == EnumToolParameterType.NULL:
        return value is None
    types = {
        EnumToolParameterType.STRING: str,
        EnumToolParameterType.BOOLEAN: bool,
        EnumToolParameterType.ARRAY: list,
        EnumToolParameterType.OBJECT: dict,
    }
    return isinstance(value, types[expected])


def _path_exists(path: str, files: set[str]) -> bool:
    """A file of the tree, or a directory holding one (a search tool's ``path``)."""
    if path in files or path in {"", "."}:
        return True
    directory = path.rstrip("/") + "/"
    return any(file.startswith(directory) for file in files)


def tool_calls_wellformed(
    request: ModelRubricCheckRequest, criterion: ModelRubricCriterion
) -> ModelRubricCriterionResult:
    transcript = request.transcript
    if transcript is None:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "no_transcript")
    params = criterion.params
    assert isinstance(params, ModelToolCallsWellformedParams)
    if not transcript.tool_calls:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "no_tool_calls")
    tools = {tool.name: tool for tool in transcript.declared_tools}
    for call in transcript.tool_calls:
        tool = tools.get(call.tool_name)
        if tool is None:
            return result(
                criterion,
                EnumRubricOutcome.FAIL,
                "undeclared_tool",
                call.call_id,
                (call.call_id,),
            )
        arguments = _arguments(call.arguments_json)
        if arguments is None:
            return result(
                criterion,
                EnumRubricOutcome.FAIL,
                "arguments_unparsable",
                call.call_id,
                (call.call_id,),
            )
        parameters = {param.name: param for param in tool.parameters}
        for param in tool.parameters:
            if param.required and param.name not in arguments:
                return result(
                    criterion,
                    EnumRubricOutcome.FAIL,
                    "missing_required_argument",
                    f"{call.call_id}:{param.name}",
                    (call.call_id, param.name),
                )
        if not params.allow_extra_arguments:
            for name in arguments:
                if name not in parameters:
                    return result(
                        criterion,
                        EnumRubricOutcome.FAIL,
                        "unknown_argument",
                        f"{call.call_id}:{name}",
                        (call.call_id, name),
                    )
        for name, value in arguments.items():
            declared = parameters.get(name)
            if declared is not None and not _matches_type(value, declared.json_type):
                return result(
                    criterion,
                    EnumRubricOutcome.FAIL,
                    "argument_type_mismatch",
                    f"{call.call_id}:{name}",
                    (call.call_id, name),
                )
    return result(
        criterion,
        EnumRubricOutcome.PASS,
        "calls_wellformed",
        facts=tuple(call.call_id for call in transcript.tool_calls),
    )


def no_phantom_paths(
    request: ModelRubricCheckRequest, criterion: ModelRubricCriterion
) -> ModelRubricCriterionResult:
    transcript = request.transcript
    if transcript is None:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "no_transcript")
    params = criterion.params
    assert isinstance(params, ModelNoPhantomPathsParams)
    if transcript.workspace_files is None:
        return result(
            criterion, EnumRubricOutcome.UNDETERMINED, "no_workspace_manifest"
        )
    known = {
        file.path.removeprefix("./"): file.line_count
        for file in transcript.workspace_files
    }
    created: set[str] = set()
    checked: list[str] = []
    unmapped: list[str] = []
    for call in transcript.tool_calls:
        arguments = _arguments(call.arguments_json)
        if arguments is None:
            continue
        for name in params.path_argument_names:
            value = arguments.get(name)
            if not isinstance(value, str):
                continue
            path = value.removeprefix("./")
            fact = f"{call.call_id}:{path}"
            if (
                call.tool_name in params.creating_tools
                and call.result is not None
                and call.result.status == EnumToolCallStatus.OK
            ):
                created.add(path)
            elif not _path_exists(path, set(known) | created):
                return result(
                    criterion, EnumRubricOutcome.FAIL, "phantom_path", fact, (fact,)
                )
            checked.append(fact)
    for citation in re.finditer(CITED_LINES_PATTERN, request.answer_text):
        path = citation["path"].removeprefix("./")
        fact = citation[0]
        if path not in known and path not in created:
            return result(
                criterion, EnumRubricOutcome.FAIL, "phantom_path", fact, (fact,)
            )
        if path not in known:
            unmapped.append(fact)
            continue
        end = int(citation["end"] or citation["start"])
        if end > known[path] + params.line_tolerance:
            return result(
                criterion, EnumRubricOutcome.FAIL, "line_past_end", fact, (fact,)
            )
        checked.append(fact)
    if unmapped:
        return result(
            criterion,
            EnumRubricOutcome.UNDETERMINED,
            "no_line_map",
            facts=tuple(unmapped),
        )
    if not checked:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "not_applicable")
    return result(
        criterion, EnumRubricOutcome.PASS, "paths_verified", facts=tuple(checked)
    )


def edits_apply(
    request: ModelRubricCheckRequest, criterion: ModelRubricCriterion
) -> ModelRubricCriterionResult:
    transcript = request.transcript
    if transcript is None:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "no_transcript")
    params = criterion.params
    assert isinstance(params, ModelEditsApplyParams)
    calls = tuple(
        call for call in transcript.tool_calls if call.tool_name in params.edit_tools
    )
    if not calls:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "not_applicable")
    for call in calls:
        if call.result is not None and call.result.status == EnumToolCallStatus.ERROR:
            return result(
                criterion,
                EnumRubricOutcome.FAIL,
                "edit_failed",
                call.call_id,
                (call.call_id,),
            )
    if any(call.result is None for call in calls):
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "no_edit_result")
    return result(
        criterion,
        EnumRubricOutcome.PASS,
        "edits_applied",
        facts=tuple(call.call_id for call in calls),
    )


def stated_check_passes(
    request: ModelRubricCheckRequest, criterion: ModelRubricCriterion
) -> ModelRubricCriterionResult:
    if request.transcript is None:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "no_transcript")
    return test_passes(request, criterion, include_request=False)


def task_answer_traceable(
    request: ModelRubricCheckRequest, criterion: ModelRubricCriterion
) -> ModelRubricCriterionResult:
    transcript = request.transcript
    if transcript is None:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "no_transcript")
    params = criterion.params
    assert isinstance(params, ModelClaimsTraceableParams)
    source = "\n".join(
        (
            request.request_text,
            *(
                call.result.output
                for call in transcript.tool_calls
                if call.result is not None
            ),
        )
    )
    units = answer_units(request.answer_text)
    missing = tuple(
        dict.fromkeys(
            token for unit in units for token in _missing(unit, source, params)
        )
    )
    if missing:
        facts = missing[: params.max_facts]
        return result(
            criterion,
            EnumRubricOutcome.FAIL,
            "untraceable_identifier",
            ", ".join(facts),
            facts,
        )
    identifiers = tuple(
        dict.fromkeys(token for unit in units for token in _missing(unit, "", params))
    )
    if not identifiers:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "no_identifiers")
    return result(
        criterion,
        EnumRubricOutcome.PASS,
        "answer_traceable",
        facts=identifiers[: params.max_facts],
    )


def within_budget(
    request: ModelRubricCheckRequest, criterion: ModelRubricCriterion
) -> ModelRubricCriterionResult:
    transcript = request.transcript
    if transcript is None:
        return result(criterion, EnumRubricOutcome.UNDETERMINED, "no_transcript")
    params = criterion.params
    assert isinstance(params, ModelWithinBudgetParams)
    facts = (
        f"turns={transcript.turn_count}",
        f"tool_calls={len(transcript.tool_calls)}",
        f"wall_time_ms={transcript.wall_time_ms if transcript.wall_time_ms is not None else 'absent'}",
    )
    exceeded: list[str] = []
    for name, value, limit in (
        ("turns", transcript.turn_count, params.max_turns),
        ("tool_calls", len(transcript.tool_calls), params.max_tool_calls),
        ("wall_time_ms", transcript.wall_time_ms, params.max_wall_time_ms),
    ):
        if value is not None and value > limit:
            exceeded.append(f"{name}={value} > {limit}")
    if exceeded:
        return result(
            criterion,
            EnumRubricOutcome.FAIL,
            "budget_exceeded",
            ", ".join(exceeded),
            facts,
        )
    if transcript.wall_time_ms is None:
        return result(
            criterion, EnumRubricOutcome.UNDETERMINED, "no_wall_time", facts=facts
        )
    return result(criterion, EnumRubricOutcome.PASS, "within_budget", facts=facts)
