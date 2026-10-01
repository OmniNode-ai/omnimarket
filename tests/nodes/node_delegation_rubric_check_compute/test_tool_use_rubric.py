# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Synthetic tool-use evidence; no captured runs or lab content."""

import json

import pytest
from pydantic import ValidationError

from omnimarket.delegation.rubric.contract_loader import load_delegation_class_rubrics
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.handler_delegation_rubric_check import (
    HandlerDelegationRubricCheck,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models import (
    EnumToolParameterType,
    ModelDeclaredTool,
    ModelEditsApplyParams,
    ModelNoPhantomPathsParams,
    ModelRubricCheckRequest,
    ModelRubricExecutionResult,
    ModelToolCall,
    ModelToolCallResult,
    ModelToolCallsWellformedParams,
    ModelToolParameter,
    ModelToolUseTranscript,
    ModelWithinBudgetParams,
    ModelWorkspaceFile,
)

pytestmark = pytest.mark.unit


def parameter(name="path", json_type="string", required=True):
    return ModelToolParameter(name=name, json_type=json_type, required=required)


def tool(name="WidgetRead", parameters=None):
    return ModelDeclaredTool(
        name=name, parameters=(parameter(),) if parameters is None else parameters
    )


def call(
    name="WidgetRead",
    arguments='{"path": "src/widget.py"}',
    status="ok",
    output="",
    call_id="call-widget",
):
    return ModelToolCall(
        call_id=call_id,
        tool_name=name,
        arguments_json=arguments,
        result=None
        if status is None
        else ModelToolCallResult(status=status, output=output),
    )


def transcript(calls=(), tools=None, files=(), turns=1, wall_time=1):
    """Build only made-up workspace and run evidence."""
    return ModelToolUseTranscript(
        declared_tools=(tool(),) if tools is None else tools,
        tool_calls=calls,
        turn_count=turns,
        wall_time_ms=wall_time,
        workspace_files=files,
    )


def request(run=None, answer="", prompt="Inspect the synthetic widget", results=()):
    rubric = load_delegation_class_rubrics().for_class("tool_use")
    # Configure invented tool names through the same typed contract parameters.
    rows = tuple(
        row.model_copy(
            update={
                "params": row.params.model_copy(
                    update={"creating_tools": ("WidgetCreate",)}
                )
            }
        )
        if row.criterion_id == "no_phantom_paths"
        else row.model_copy(
            update={
                "params": row.params.model_copy(update={"edit_tools": ("WidgetEdit",)})
            }
        )
        if row.criterion_id == "edits_apply"
        else row
        for row in rubric.criteria
    )
    return ModelRubricCheckRequest(
        task_class="tool_use",
        request_text=prompt,
        answer_text=answer,
        rubric=rubric.model_copy(update={"criteria": rows}),
        transcript=run,
        execution_results=results,
    )


def criterion(criterion_id, item=None, **kwargs):
    verdict = HandlerDelegationRubricCheck().handle(item or request(**kwargs))
    return next(row for row in verdict.criteria if row.criterion_id == criterion_id)


def replace_params(item, criterion_id, updates):
    rows = tuple(
        row.model_copy(update={"params": row.params.model_copy(update=updates)})
        if row.criterion_id == criterion_id
        else row
        for row in item.rubric.criteria
    )
    return item.model_copy(
        update={"rubric": item.rubric.model_copy(update={"criteria": rows})}
    )


def test_tool_use_wellformed_pass():
    row = criterion(
        "tool_calls_wellformed",
        run=transcript(calls=(call(), call(call_id="call-other"))),
    )
    assert (row.outcome, row.reason_code, row.facts) == (
        "PASS",
        "calls_wellformed",
        ("call-widget", "call-other"),
    )


def test_tool_use_wellformed_undeclared_tool():
    row = criterion(
        "tool_calls_wellformed", run=transcript(calls=(call(name="WidgetUnknown"),))
    )
    assert (row.outcome, row.reason_code) == ("FAIL", "undeclared_tool")
    assert "call-widget" in row.detail
    assert "call-widget" in row.facts


@pytest.mark.parametrize("arguments", ["{broken", "", "not JSON"])
def test_tool_use_wellformed_unparsable_text(arguments):
    row = criterion(
        "tool_calls_wellformed", run=transcript(calls=(call(arguments=arguments),))
    )
    assert (row.outcome, row.reason_code) == ("FAIL", "arguments_unparsable")
    assert row.detail == "call-widget"
    assert row.facts == ("call-widget",)


@pytest.mark.parametrize("arguments", ["[]", "null", "true", "1", '"widget"'])
def test_tool_use_wellformed_non_object_json(arguments):
    row = criterion(
        "tool_calls_wellformed", run=transcript(calls=(call(arguments=arguments),))
    )
    assert (row.outcome, row.reason_code) == ("FAIL", "arguments_unparsable")


def test_tool_use_wellformed_missing_required():
    row = criterion(
        "tool_calls_wellformed", run=transcript(calls=(call(arguments="{}"),))
    )
    assert (row.outcome, row.reason_code, row.detail) == (
        "FAIL",
        "missing_required_argument",
        "call-widget:path",
    )
    assert "call-widget" in row.facts


def test_tool_use_wellformed_unknown_argument():
    item = request(
        run=transcript(calls=(call(arguments='{"path": "src/widget.py", "other": 1}'),))
    )
    row = criterion("tool_calls_wellformed", item)
    assert (row.outcome, row.reason_code, row.detail) == (
        "FAIL",
        "unknown_argument",
        "call-widget:other",
    )
    assert "call-widget" in row.facts
    assert (
        criterion(
            "tool_calls_wellformed",
            replace_params(
                item, "tool_calls_wellformed", {"allow_extra_arguments": True}
            ),
        ).outcome
        == "PASS"
    )


@pytest.mark.parametrize(
    ("json_type", "value"),
    [
        ("string", "1"),
        ("integer", "1.5"),
        ("integer", "true"),
        ("number", "false"),
        ("number", '"1"'),
        ("boolean", "1"),
        ("array", "{}"),
        ("object", "[]"),
        ("null", '"null"'),
    ],
)
def test_tool_use_wellformed_type_mismatch(json_type, value):
    run = transcript(
        tools=(tool(parameters=(parameter("value", json_type),)),),
        calls=(call(arguments='{"value": ' + value + "}"),),
    )
    row = criterion("tool_calls_wellformed", run=run)
    assert (row.outcome, row.reason_code, row.detail) == (
        "FAIL",
        "argument_type_mismatch",
        "call-widget:value",
    )
    assert "call-widget" in row.facts


@pytest.mark.parametrize(
    ("json_type", "value"),
    [
        ("string", '"widget"'),
        ("integer", "1"),
        ("number", "1"),
        ("number", "1.5"),
        ("boolean", "true"),
        ("array", "[]"),
        ("object", "{}"),
        ("null", "null"),
    ],
)
def test_tool_use_wellformed_each_type_pass(json_type, value):
    run = transcript(
        tools=(tool(parameters=(parameter("value", json_type),)),),
        calls=(call(arguments='{"value": ' + value + "}"),),
    )
    assert criterion("tool_calls_wellformed", run=run).outcome == "PASS"


def test_tool_use_wellformed_optional_parameter_pass():
    run = transcript(
        tools=(tool(parameters=(parameter(required=False),)),),
        calls=(call(arguments="{}"),),
    )
    assert criterion("tool_calls_wellformed", run=run).outcome == "PASS"


def test_tool_use_wellformed_first_call_failure_wins():
    run = transcript(
        calls=(call(arguments="{}"), call(name="WidgetUnknown", call_id="call-other"))
    )
    assert criterion("tool_calls_wellformed", run=run).detail == "call-widget:path"


def test_tool_use_wellformed_no_calls():
    row = criterion("tool_calls_wellformed", run=transcript())
    assert (row.outcome, row.reason_code) == ("UNDETERMINED", "no_tool_calls")


def test_tool_use_phantom_paths_pass():
    run = transcript(
        calls=(call(),),
        files=(ModelWorkspaceFile(path="src/widget.py", line_count=12),),
    )
    row = criterion("no_phantom_paths", run=run, answer="src/widget.py:1-12")
    assert (row.outcome, row.reason_code, row.facts) == (
        "PASS",
        "paths_verified",
        ("call-widget:src/widget.py", "src/widget.py:1-12"),
    )


@pytest.mark.parametrize("directory", ["src", "src/", "./src", "."])
def test_tool_use_phantom_paths_directory_of_known_file_passes(directory):
    run = transcript(
        calls=(call(arguments='{"path": "' + directory + '"}'),),
        files=(ModelWorkspaceFile(path="src/widget.py", line_count=12),),
    )
    assert criterion("no_phantom_paths", run=run).outcome == "PASS"


@pytest.mark.parametrize("directory", ["lib", "sr", "src/widget"])
def test_tool_use_phantom_paths_directory_holding_no_file_fails(directory):
    run = transcript(
        calls=(call(arguments='{"path": "' + directory + '"}'),),
        files=(ModelWorkspaceFile(path="src/widget.py", line_count=12),),
    )
    row = criterion("no_phantom_paths", run=run)
    assert (row.outcome, row.reason_code) == ("FAIL", "phantom_path")


def test_tool_use_phantom_paths_phantom_read():
    row = criterion("no_phantom_paths", run=transcript(calls=(call(),)))
    assert (row.outcome, row.reason_code, row.detail) == (
        "FAIL",
        "phantom_path",
        "call-widget:src/widget.py",
    )


def test_tool_use_phantom_paths_created_then_read_passes():
    run = transcript(calls=(call(name="WidgetCreate"), call(call_id="call-read")))
    assert criterion("no_phantom_paths", run=run).outcome == "PASS"


@pytest.mark.parametrize("status", ["error", None])
def test_tool_use_phantom_paths_failed_creation_stays_phantom(status):
    run = transcript(
        calls=(call(name="WidgetCreate", status=status), call(call_id="call-read"))
    )
    row = criterion("no_phantom_paths", run=run)
    assert (row.outcome, row.reason_code, row.detail) == (
        "FAIL",
        "phantom_path",
        "call-widget:src/widget.py",
    )


def test_tool_use_phantom_paths_read_before_creation_fails():
    run = transcript(calls=(call(), call(name="WidgetCreate", call_id="call-create")))
    assert criterion("no_phantom_paths", run=run).detail == "call-widget:src/widget.py"


def test_tool_use_phantom_paths_cited_line_past_end_fails():
    run = transcript(files=(ModelWorkspaceFile(path="src/widget.py", line_count=12),))
    row = criterion("no_phantom_paths", run=run, answer="src/widget.py:1-13")
    assert (row.outcome, row.reason_code) == ("FAIL", "line_past_end")


def test_tool_use_phantom_paths_cited_unknown_file_fails():
    row = criterion("no_phantom_paths", run=transcript(), answer="src/widget.py:1")
    assert (row.outcome, row.reason_code) == ("FAIL", "phantom_path")


def test_tool_use_phantom_paths_line_tolerance_from_contract():
    item = request(
        run=transcript(
            files=(ModelWorkspaceFile(path="src/widget.py", line_count=12),)
        ),
        answer="src/widget.py:13",
    )
    assert criterion("no_phantom_paths", item).outcome == "FAIL"
    assert (
        criterion(
            "no_phantom_paths",
            replace_params(item, "no_phantom_paths", {"line_tolerance": 1}),
        ).outcome
        == "PASS"
    )


def test_tool_use_phantom_paths_created_citation_undetermined():
    run = transcript(calls=(call(name="WidgetCreate"),))
    row = criterion("no_phantom_paths", run=run, answer="src/widget.py:1")
    assert (row.outcome, row.reason_code, row.facts) == (
        "UNDETERMINED",
        "no_line_map",
        ("src/widget.py:1",),
    )


def test_tool_use_phantom_paths_failure_overrides_unknown_line_map():
    run = transcript(calls=(call(name="WidgetCreate"),))
    row = criterion(
        "no_phantom_paths", run=run, answer="src/widget.py:1 src/absent.py:1"
    )
    assert (row.outcome, row.reason_code) == ("FAIL", "phantom_path")


def test_tool_use_phantom_paths_no_manifest_undetermined():
    row = criterion("no_phantom_paths", run=transcript(files=None))
    assert (row.outcome, row.reason_code) == ("UNDETERMINED", "no_workspace_manifest")


@pytest.mark.parametrize(
    "arguments", ["{}", "{broken", "[]", '{"path": 1}', '{"other": "src/widget.py"}']
)
def test_tool_use_phantom_paths_nothing_to_check_undetermined(arguments):
    row = criterion(
        "no_phantom_paths", run=transcript(calls=(call(arguments=arguments),))
    )
    assert (row.outcome, row.reason_code) == ("UNDETERMINED", "not_applicable")


@pytest.mark.parametrize(
    ("manifest_path", "argument_path", "citation_path"),
    [
        ("./src/widget.py", "src/widget.py", "src/widget.py"),
        ("src/widget.py", "./src/widget.py", "./src/widget.py"),
    ],
)
def test_tool_use_phantom_paths_leading_dot_normalised(
    manifest_path, argument_path, citation_path
):
    run = transcript(
        calls=(call(arguments='{"path": "' + argument_path + '"}'),),
        files=(ModelWorkspaceFile(path=manifest_path, line_count=1),),
    )
    assert (
        criterion("no_phantom_paths", run=run, answer=citation_path + ":1").outcome
        == "PASS"
    )


def test_tool_use_phantom_paths_created_leading_dot_normalised():
    run = transcript(
        calls=(
            call(name="WidgetCreate", arguments='{"path": "./src/widget.py"}'),
            call(call_id="call-read"),
        )
    )
    assert criterion("no_phantom_paths", run=run).outcome == "PASS"


@pytest.mark.parametrize("argument_name", ["path", "file_path", "notebook_path"])
def test_tool_use_phantom_paths_configured_argument_names(argument_name):
    row = criterion(
        "no_phantom_paths",
        run=transcript(
            calls=(call(arguments='{"' + argument_name + '": "src/widget.py"}'),)
        ),
    )
    assert row.reason_code == "phantom_path"


def test_tool_use_edits_apply_pass():
    row = criterion("edits_apply", run=transcript(calls=(call(name="WidgetEdit"),)))
    assert (row.outcome, row.reason_code) == ("PASS", "edits_applied")


def test_tool_use_edits_apply_error_fails():
    row = criterion(
        "edits_apply", run=transcript(calls=(call(name="WidgetEdit", status="error"),))
    )
    assert (row.outcome, row.reason_code, row.detail) == (
        "FAIL",
        "edit_failed",
        "call-widget",
    )


def test_tool_use_edits_apply_missing_result_undetermined():
    row = criterion(
        "edits_apply", run=transcript(calls=(call(name="WidgetEdit", status=None),))
    )
    assert (row.outcome, row.reason_code) == ("UNDETERMINED", "no_edit_result")


def test_tool_use_edits_apply_failure_overrides_missing_result():
    run = transcript(
        calls=(
            call(name="WidgetEdit", status=None),
            call(name="WidgetEdit", status="error", call_id="call-other"),
        )
    )
    assert criterion("edits_apply", run=run).reason_code == "edit_failed"


def test_tool_use_edits_apply_no_edits_not_applicable():
    row = criterion("edits_apply", run=transcript(calls=(call(),)))
    assert (row.outcome, row.reason_code) == ("UNDETERMINED", "not_applicable")


@pytest.mark.parametrize(
    ("passed", "outcome", "reason"),
    [
        (True, "PASS", "tests_verified"),
        (False, "FAIL", "test_failed"),
        (None, "UNDETERMINED", "no_execution_result"),
    ],
)
def test_tool_use_stated_check_named_result(passed, outcome, reason):
    results = (
        ()
        if passed is None
        else (ModelRubricExecutionResult(target="test_widget", passed=passed),)
    )
    row = criterion(
        "stated_check_passes", run=transcript(), answer="test_widget", results=results
    )
    assert (row.outcome, row.reason_code) == (outcome, reason)


def test_tool_use_stated_check_request_alone_not_applicable():
    row = criterion(
        "stated_check_passes", run=transcript(), prompt="Run test_widget", answer="Done"
    )
    assert (row.outcome, row.reason_code) == ("UNDETERMINED", "not_applicable")


def test_tool_use_answer_traceable_tool_output_passes():
    run = transcript(
        calls=(call(output="SYN-101 built src/widget.py with widget_value"),)
    )
    row = criterion(
        "task_answer_traceable",
        run=run,
        answer="SYN-101 built `widget_value` in src/widget.py.",
    )
    assert (row.outcome, row.reason_code) == ("PASS", "answer_traceable")


def test_tool_use_answer_traceable_request_passes():
    row = criterion(
        "task_answer_traceable",
        run=transcript(),
        prompt="Inspect SYN-101",
        answer="SYN-101 inspected.",
    )
    assert (row.outcome, row.reason_code) == ("PASS", "answer_traceable")


def test_tool_use_answer_traceable_accepted_call_argument_passes():
    run = transcript(calls=(call(output="def build_widget(): ..."),))
    row = criterion(
        "task_answer_traceable", run=run, answer="Changed src/widget.py as asked."
    )
    assert (row.outcome, row.reason_code) == ("PASS", "answer_traceable")


def test_tool_use_answer_traceable_reads_decoded_argument_text():
    edit = call(
        name="WidgetEdit",
        arguments=json.dumps(
            {"path": "src/widget.py", "new": 'config = ConfigDict(extra="forbid")'}
        ),
    )
    row = criterion(
        "task_answer_traceable",
        run=transcript(calls=(edit,)),
        answer='Set `config = ConfigDict(extra="forbid")` in src/widget.py.',
    )
    assert (row.outcome, row.reason_code) == ("PASS", "answer_traceable")


@pytest.mark.parametrize("status", ["error", None])
def test_tool_use_answer_traceable_refused_call_argument_is_not_evidence(status):
    run = transcript(calls=(call(status=status, output="no such file"),))
    row = criterion(
        "task_answer_traceable", run=run, answer="Changed src/widget.py as asked."
    )
    assert (row.outcome, row.reason_code, row.facts) == (
        "FAIL",
        "untraceable_identifier",
        ("src/widget.py",),
    )


def test_tool_use_answer_traceable_identifier_nowhere_fails_and_named():
    row = criterion(
        "task_answer_traceable", run=transcript(), answer="SYN-999 changed."
    )
    assert (row.outcome, row.reason_code, row.facts) == (
        "FAIL",
        "untraceable_identifier",
        ("SYN-999",),
    )
    assert "SYN-999" in row.detail


def test_tool_use_answer_traceable_source_context_is_not_tool_evidence():
    item = request(run=transcript(), answer="SYN-999 changed.").model_copy(
        update={"source_text": "SYN-999"}
    )
    assert criterion("task_answer_traceable", item).outcome == "FAIL"


def test_tool_use_answer_traceable_no_identifiers_undetermined():
    row = criterion(
        "task_answer_traceable", run=transcript(), answer="Everything improved."
    )
    assert (row.outcome, row.reason_code) == ("UNDETERMINED", "no_identifiers")


def test_tool_use_answer_traceable_list_markers_and_qualified_refs():
    row = criterion(
        "task_answer_traceable",
        run=transcript(),
        prompt="widget#42 built SYN-101",
        answer="40. SYN-101 shipped with #42.",
    )
    assert row.outcome == "PASS"


def test_tool_use_answer_traceable_missing_facts_bounded():
    item = replace_params(
        request(run=transcript(), answer="SYN-901. SYN-902. SYN-903."),
        "task_answer_traceable",
        {"max_facts": 1},
    )
    row = criterion("task_answer_traceable", item)
    assert row.facts == ("SYN-901",)
    assert row.detail == "SYN-901"


@pytest.mark.parametrize(
    ("turns", "wall_time", "outcome"),
    [(1, 1, "PASS"), (41, 1, "FAIL"), (1, None, "UNDETERMINED"), (41, None, "FAIL")],
)
def test_tool_use_budget_facts_always_present_in_order(turns, wall_time, outcome):
    row = criterion(
        "within_budget",
        run=transcript(calls=(call(),), turns=turns, wall_time=wall_time),
    )
    assert row.outcome == outcome
    assert row.facts == (
        f"turns={turns}",
        "tool_calls=1",
        f"wall_time_ms={wall_time if wall_time is not None else 'absent'}",
    )


@pytest.mark.parametrize(
    ("field", "limit"), [("turns", 40), ("tool_calls", 80), ("wall_time_ms", 900000)]
)
def test_tool_use_budget_each_limit_exceeded_fails(field, limit):
    run = transcript(
        turns=limit + 1 if field == "turns" else 1,
        wall_time=limit + 1 if field == "wall_time_ms" else 1,
        calls=tuple(call(call_id=f"call-{n}") for n in range(limit + 1))
        if field == "tool_calls"
        else (),
    )
    row = criterion("within_budget", run=run)
    assert (row.outcome, row.reason_code) == ("FAIL", "budget_exceeded")
    assert field in row.detail
    assert str(limit) in row.detail


def test_tool_use_budget_all_exceeded_fields_named():
    row = criterion(
        "within_budget",
        run=transcript(
            turns=41,
            wall_time=900001,
            calls=tuple(call(call_id=f"call-{n}") for n in range(81)),
        ),
    )
    assert (
        row.detail == "turns=41 > 40, tool_calls=81 > 80, wall_time_ms=900001 > 900000"
    )


def test_tool_use_budget_absent_wall_time_undetermined():
    row = criterion("within_budget", run=transcript(wall_time=None))
    assert (row.outcome, row.reason_code) == ("UNDETERMINED", "no_wall_time")


def test_tool_use_budget_all_within_passes():
    row = criterion(
        "within_budget",
        run=transcript(
            turns=40,
            wall_time=900000,
            calls=tuple(call(call_id=f"call-{n}") for n in range(80)),
        ),
    )
    assert (row.outcome, row.reason_code) == ("PASS", "within_budget")


def test_tool_use_no_transcript_never_pass():
    item = request(
        answer="SYN-101 test_widget",
        prompt="SYN-101",
        results=(ModelRubricExecutionResult(target="test_widget", passed=True),),
    )
    verdict = HandlerDelegationRubricCheck().handle(item)
    assert tuple(row.criterion_id for row in verdict.criteria) == (
        "tool_calls_wellformed",
        "no_phantom_paths",
        "edits_apply",
        "stated_check_passes",
        "task_answer_traceable",
        "within_budget",
    )
    assert all(
        (row.outcome, row.reason_code) == ("UNDETERMINED", "no_transcript")
        for row in verdict.criteria
    )
    assert verdict.outcome == "UNDETERMINED"


def test_tool_use_no_literal_thresholds():
    item = request(
        run=transcript(
            turns=41, files=(ModelWorkspaceFile(path="src/widget.py", line_count=12),)
        ),
        answer="src/widget.py:13",
    )
    assert criterion("within_budget", item).outcome == "FAIL"
    assert criterion("no_phantom_paths", item).outcome == "FAIL"
    tolerant = replace_params(
        replace_params(item, "within_budget", {"max_turns": 41}),
        "no_phantom_paths",
        {"line_tolerance": 1},
    )
    assert criterion("within_budget", tolerant).outcome == "PASS"
    assert criterion("no_phantom_paths", tolerant).outcome == "PASS"


def test_tool_use_transcript_models_forbid_extra_and_duplicates():
    instances = (
        parameter(),
        tool(),
        call().result,
        call(),
        ModelWorkspaceFile(path="src/widget.py", line_count=1),
        transcript(),
        ModelToolCallsWellformedParams(allow_extra_arguments=False),
        ModelNoPhantomPathsParams(
            path_argument_names=("path",), creating_tools=(), line_tolerance=0
        ),
        ModelEditsApplyParams(edit_tools=("WidgetEdit",)),
        ModelWithinBudgetParams(max_turns=1, max_tool_calls=1, max_wall_time_ms=1),
    )
    for instance in instances:
        assert instance is not None
        with pytest.raises(ValidationError):
            type(instance).model_validate(
                {**instance.model_dump(), "unknown": "synthetic"}
            )
        field = next(iter(type(instance).model_fields))
        with pytest.raises(ValidationError):
            setattr(instance, field, getattr(instance, field))
    with pytest.raises(ValidationError, match="duplicate parameter names"):
        tool(parameters=(parameter(), parameter()))
    with pytest.raises(ValidationError, match="duplicate tool names"):
        transcript(tools=(tool(), tool()))
    with pytest.raises(ValidationError, match="duplicate call ids"):
        transcript(calls=(call(), call()))
    with pytest.raises(ValidationError, match="duplicate workspace paths"):
        transcript(files=(ModelWorkspaceFile(path="src/widget.py", line_count=1),) * 2)
    assert set(EnumToolParameterType) == {
        "string",
        "integer",
        "number",
        "boolean",
        "array",
        "object",
        "null",
    }


@pytest.mark.parametrize(
    ("model", "data"),
    [
        (ModelToolParameter, {"name": "", "json_type": "string", "required": True}),
        (ModelDeclaredTool, {"name": "", "parameters": ()}),
        (
            ModelToolCall,
            {"call_id": "", "tool_name": "WidgetRead", "arguments_json": "{}"},
        ),
        (
            ModelToolCall,
            {"call_id": "call-widget", "tool_name": "", "arguments_json": "{}"},
        ),
        (ModelWorkspaceFile, {"path": "", "line_count": 0}),
        (ModelWorkspaceFile, {"path": "src/widget.py", "line_count": -1}),
        (
            ModelToolUseTranscript,
            {"declared_tools": (), "tool_calls": (), "turn_count": -1},
        ),
        (
            ModelToolUseTranscript,
            {
                "declared_tools": (),
                "tool_calls": (),
                "turn_count": 0,
                "wall_time_ms": -1,
            },
        ),
        (
            ModelNoPhantomPathsParams,
            {"path_argument_names": (), "creating_tools": (), "line_tolerance": 0},
        ),
        (
            ModelNoPhantomPathsParams,
            {
                "path_argument_names": ("path",),
                "creating_tools": (),
                "line_tolerance": -1,
            },
        ),
        (ModelEditsApplyParams, {"edit_tools": ()}),
        (
            ModelWithinBudgetParams,
            {"max_turns": 0, "max_tool_calls": 1, "max_wall_time_ms": 1},
        ),
        (
            ModelWithinBudgetParams,
            {"max_turns": 1, "max_tool_calls": 0, "max_wall_time_ms": 1},
        ),
        (
            ModelWithinBudgetParams,
            {"max_turns": 1, "max_tool_calls": 1, "max_wall_time_ms": 0},
        ),
    ],
)
def test_tool_use_transcript_models_reject_invalid_fields(model, data):
    with pytest.raises(ValidationError):
        model.model_validate(data)


def test_tool_use_transcript_models_preserve_raw_arguments_and_manifest_absence():
    raw = '  { "path" : "src/widget.py" }\n'
    assert call(arguments=raw).arguments_json == raw
    assert transcript(files=None).workspace_files is None
    assert transcript(files=()).workspace_files == ()
    assert transcript(turns=0, wall_time=0).turn_count == 0
