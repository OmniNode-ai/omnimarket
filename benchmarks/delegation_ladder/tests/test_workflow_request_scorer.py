# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The R3b scorer measures parameter filling, not substring presence (OMN-18339).

Every acceptance criterion of the ticket has a falsifier here:

* AC1  a response naming the right workflow whose parameter object omits a
       required field, or types one wrongly, FAILS.
* AC2  a patch must be present and must apply; a plausible diff that does not
       apply FAILS and one that applies PASSES.
* AC3  the review counterexample (the old accepted lists, space-joined) is
       committed verbatim and FAILS for every R3b task; the retired substring
       scorer is shown passing the same text, which is the control.
* AC5  no numeric literal enters the new scorer's code path.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from benchmarks.delegation_ladder.models import (
    EnumOutcome,
    ModelScore,
    ModelTaskBundle,
)
from benchmarks.delegation_ladder.run_ladder import score
from benchmarks.delegation_ladder.scorers import score_exact_match
from omnimarket.handlers import workflow_request_score
from omnimarket.handlers.workflow_request_score import parse_workflow_request

HERE = Path(__file__).resolve().parent.parent
FIXTURES = HERE / "fixtures"
BUNDLES = HERE / "bundles"

pytestmark = pytest.mark.unit

R3B_IDS = ("R3b-01", "R3b-02", "R3b-03", "R3b-04")


def _bundle(task_id: str) -> ModelTaskBundle:
    return ModelTaskBundle.model_validate(
        json.loads((BUNDLES / f"{task_id}.json").read_text(encoding="utf-8"))
    )


def _score(task_id: str, response: str) -> EnumOutcome:
    return _result(task_id, response).outcome


def _result(task_id: str, response: str) -> ModelScore:
    return score(_bundle(task_id), response)


def _answer(workflow: str, params: dict[str, object]) -> str:
    return workflow + "\n" + json.dumps(params)


GOOD_01 = _answer(
    "run_focused_tests",
    {
        "repo": "omnibase_core",
        "path": "tests/unit/validation/test_occ_merge_eligibility.py",
        "k_expr": None,
    },
)
GOOD_02 = _answer(
    "reproduce_failing_run", {"run_id": 34748103920, "repo": "omnibase_infra"}
)
GOOD_03 = _answer(
    "read_and_answer",
    {
        "paths": ["scripts/check_dockerfile_pins.py"],
        "question": "What does _satisfies_specifier return for 2.0.0?",
        "repo": "omnibase_infra",
    },
)
APPLYING_PATCH = (FIXTURES / "r3b_04_patch_applies.diff").read_text(encoding="utf-8")
NON_APPLYING_PATCH = (FIXTURES / "r3b_04_patch_does_not_apply.diff").read_text(
    encoding="utf-8"
)
GOOD_04 = _answer(
    "apply_patch_and_test",
    {
        "repo": "omnimarket",
        "patch": APPLYING_PATCH,
        "test_path": "tests/unit/cost/test_usage_normalizer_omn13621.py",
    },
)


# -- the scorer wiring --------------------------------------------------------


def test_every_r3b_bundle_is_scored_by_the_workflow_request_scorer() -> None:
    for task_id in R3B_IDS:
        assert _bundle(task_id).scorer.value == "workflow_request"


@pytest.mark.parametrize(
    ("task_id", "response"),
    [
        ("R3b-01", GOOD_01),
        ("R3b-02", GOOD_02),
        ("R3b-03", GOOD_03),
        ("R3b-04", GOOD_04),
    ],
)
def test_a_complete_type_correct_request_passes(task_id: str, response: str) -> None:
    result = _result(task_id, response)
    assert result.outcome is EnumOutcome.PASS, result.detail


def test_a_fenced_json_object_and_a_backticked_name_are_read() -> None:
    fenced = "`run_focused_tests`\n```json\n" + GOOD_01.split("\n", 1)[1] + "\n```"
    assert _score("R3b-01", fenced) is EnumOutcome.PASS


def test_an_unrelated_leading_reasoning_trace_is_not_the_answer() -> None:
    traced = "<think>maybe read_and_answer</think>\n" + GOOD_01
    assert _score("R3b-01", traced) is EnumOutcome.PASS


# -- AC1: required parameters and types --------------------------------------


def test_ac1_omitting_a_required_parameter_fails() -> None:
    missing_path = _answer("run_focused_tests", {"repo": "omnibase_core"})
    result = _result("R3b-01", missing_path)
    assert result.outcome is EnumOutcome.FAIL
    assert result.checklist["parameters_validate_against_request_schema"] is False
    assert result.checklist["parameter:path"] is False


def test_ac1_a_wrong_type_fails_for_the_run_id() -> None:
    as_string = _answer(
        "reproduce_failing_run", {"run_id": "34748103920", "repo": "omnibase_infra"}
    )
    result = _result("R3b-02", as_string)
    assert result.outcome is EnumOutcome.FAIL
    assert result.checklist["parameters_validate_against_request_schema"] is False


def test_ac1_a_boolean_is_not_an_integer_run_id() -> None:
    boolean = _answer(
        "reproduce_failing_run", {"run_id": True, "repo": "omnibase_infra"}
    )
    assert _score("R3b-02", boolean) is EnumOutcome.FAIL


def test_ac1_a_scalar_where_a_list_is_required_fails() -> None:
    scalar_paths = _answer(
        "read_and_answer",
        {
            "paths": "scripts/check_dockerfile_pins.py",
            "question": "What does it return?",
            "repo": "omnibase_infra",
        },
    )
    assert _score("R3b-03", scalar_paths) is EnumOutcome.FAIL


def test_ac1_a_missing_question_fails_even_with_every_held_value_present() -> None:
    no_question = _answer(
        "read_and_answer",
        {"paths": ["scripts/check_dockerfile_pins.py"], "repo": "omnibase_infra"},
    )
    result = _result("R3b-03", no_question)
    assert result.outcome is EnumOutcome.FAIL
    assert result.checklist["parameter:paths"] is True
    assert result.checklist["parameter:repo"] is True
    assert result.checklist["parameters_validate_against_request_schema"] is False


def test_ac1_a_parameter_the_catalogue_lacks_fails() -> None:
    extra = _answer(
        "run_focused_tests",
        {
            "repo": "omnibase_core",
            "path": "tests/unit/validation/test_occ_merge_eligibility.py",
            "flags": "-x",
        },
    )
    assert _score("R3b-01", extra) is EnumOutcome.FAIL


def test_ac1_a_repo_outside_the_catalogue_enum_fails() -> None:
    wrong_repo = _answer(
        "run_focused_tests",
        {
            "repo": "not_a_repo",
            "path": "tests/unit/validation/test_occ_merge_eligibility.py",
        },
    )
    assert _score("R3b-01", wrong_repo) is EnumOutcome.FAIL


def test_ac1_the_wrong_workflow_fails_even_with_a_valid_object() -> None:
    wrong = _answer(
        "select_impacted_tests",
        {"repo": "omnibase_core", "changed_paths": ["src/x.py"]},
    )
    result = _result("R3b-01", wrong)
    assert result.outcome is EnumOutcome.FAIL
    assert result.checklist["parameters_validate_against_request_schema"] is True
    assert result.checklist["names_the_held_workflow"] is False


def test_ac1_a_workflow_outside_the_catalogue_fails() -> None:
    invented = _answer("run_all_the_tests", {"repo": "omnibase_core"})
    result = _result("R3b-01", invented)
    assert result.outcome is EnumOutcome.FAIL
    assert result.checklist["names_a_catalogue_workflow"] is False


def test_ac1_a_wrong_held_value_fails() -> None:
    wrong_path = _answer(
        "run_focused_tests",
        {"repo": "omnibase_core", "path": "tests/unit/validation/other.py"},
    )
    result = _result("R3b-01", wrong_path)
    assert result.outcome is EnumOutcome.FAIL
    assert result.checklist["parameters_validate_against_request_schema"] is True
    assert result.checklist["parameter:path"] is False


@pytest.mark.parametrize(
    "response",
    [
        "",
        "run_focused_tests",
        "run_focused_tests\n{not json",
        "I would run the focused tests: " + GOOD_01,
        "run_focused_tests\n[1, 2, 3]",
    ],
)
def test_ac1_no_object_or_unparseable_text_fails(response: str) -> None:
    assert _score("R3b-01", response) is EnumOutcome.FAIL


# -- AC2: a patch must be present and must apply -----------------------------


def test_ac2_a_diff_that_applies_to_the_named_file_passes() -> None:
    result = _result("R3b-04", GOOD_04)
    assert result.outcome is EnumOutcome.PASS, result.detail
    assert result.checklist["patch_is_present"] is True
    assert result.checklist["patch_applies"] is True


def test_ac2_a_plausible_diff_that_does_not_apply_fails() -> None:
    plausible = _answer(
        "apply_patch_and_test",
        {
            "repo": "omnimarket",
            "patch": NON_APPLYING_PATCH,
            "test_path": "tests/unit/cost/test_usage_normalizer_omn13621.py",
        },
    )
    result = _result("R3b-04", plausible)
    assert result.outcome is EnumOutcome.FAIL
    assert result.checklist["patch_is_present"] is True
    assert result.checklist["patch_applies"] is False
    # Everything the substring scorer looked at is right; only the patch is not.
    assert result.checklist["names_the_held_workflow"] is True
    assert result.checklist["parameter:test_path"] is True


def test_ac2_the_non_applying_fixture_is_a_diff_not_noise() -> None:
    """Control: the failing fixture parses as a unified diff; it fails on context."""
    assert NON_APPLYING_PATCH.startswith(
        "--- a/src/omnimarket/cost/usage_normalizer.py"
    )
    assert re.search(r"^@@ -\d+,\d+ \+\d+,\d+ @@", NON_APPLYING_PATCH, re.MULTILINE)


@pytest.mark.parametrize("patch", ["", "   \n", None])
def test_ac2_an_absent_or_empty_patch_fails(patch: object) -> None:
    params: dict[str, object] = {
        "repo": "omnimarket",
        "test_path": "tests/unit/cost/test_usage_normalizer_omn13621.py",
    }
    if patch is not None:
        params["patch"] = patch
    result = _result("R3b-04", _answer("apply_patch_and_test", params))
    assert result.outcome is EnumOutcome.FAIL
    assert result.checklist["patch_is_present"] is False
    assert result.checklist["patch_applies"] is False


def test_ac2_prose_in_place_of_a_diff_fails() -> None:
    prose = _answer(
        "apply_patch_and_test",
        {
            "repo": "omnimarket",
            "patch": "Add a docstring to _estimate_from_text.",
            "test_path": "tests/unit/cost/test_usage_normalizer_omn13621.py",
        },
    )
    result = _result("R3b-04", prose)
    assert result.outcome is EnumOutcome.FAIL
    assert result.checklist["patch_applies"] is False


def test_ac2_a_diff_that_also_touches_another_file_fails() -> None:
    other = (
        "--- a/src/omnimarket/cost/other.py\n+++ b/src/omnimarket/cost/other.py\n"
        "@@ -0,0 +1 @@\n+x = 1\n"
    )
    response = _answer(
        "apply_patch_and_test",
        {
            "repo": "omnimarket",
            "patch": APPLYING_PATCH + other,
            "test_path": "tests/unit/cost/test_usage_normalizer_omn13621.py",
        },
    )
    assert _score("R3b-04", response) is EnumOutcome.FAIL


def test_ac2_a_missing_final_newline_in_the_json_string_is_tolerated() -> None:
    response = _answer(
        "apply_patch_and_test",
        {
            "repo": "omnimarket",
            "patch": APPLYING_PATCH.rstrip("\n"),
            "test_path": "tests/unit/cost/test_usage_normalizer_omn13621.py",
        },
    )
    assert _score("R3b-04", response) is EnumOutcome.PASS


def test_ac2_the_prompt_carries_the_diff_the_model_is_to_fill_in() -> None:
    """A task whose patch cannot be known is unmeasurable, not hard."""
    assert APPLYING_PATCH in _bundle("R3b-04").prompt


# -- AC3: the review counterexample is a committed RED fixture ---------------


def _counterexamples() -> dict[str, object]:
    data: dict[str, object] = json.loads(
        (FIXTURES / "r3b_review_counterexamples.json").read_text(encoding="utf-8")
    )
    return data


def test_ac3_the_counterexample_lists_are_the_old_accepted_lists_space_joined() -> None:
    data = _counterexamples()
    responses = data["responses"]
    legacy = data["legacy_exact_match"]
    assert isinstance(responses, dict)
    assert isinstance(legacy, dict)
    assert sorted(responses) == list(R3B_IDS)
    for task_id in R3B_IDS:
        assert responses[task_id] == " ".join(legacy[task_id]["accepted"])
        assert "{" not in responses[task_id]


@pytest.mark.parametrize("task_id", R3B_IDS)
def test_ac3_each_counterexample_fails_under_the_new_scorer(task_id: str) -> None:
    """If this goes green the scorer has regressed to substring matching."""
    responses = _counterexamples()["responses"]
    assert isinstance(responses, dict)
    result = _result(task_id, responses[task_id])
    assert result.outcome is EnumOutcome.FAIL
    assert result.checklist["parameters_are_a_json_object"] is False


@pytest.mark.parametrize("task_id", R3B_IDS)
def test_ac3_control_the_retired_substring_scorer_passes_the_same_text(
    task_id: str,
) -> None:
    data = _counterexamples()
    responses = data["responses"]
    legacy = data["legacy_exact_match"]
    assert isinstance(responses, dict)
    assert isinstance(legacy, dict)
    assert (
        score_exact_match(responses[task_id], legacy[task_id]).outcome
        is EnumOutcome.PASS
    )


# -- the schemas are the catalogue --------------------------------------------


def _catalogue_parameters() -> dict[str, dict[str, str]]:
    """``{workflow: {parameter: type words}}`` read off workflow_catalogue.txt."""
    parsed: dict[str, dict[str, str]] = {}
    current = ""
    in_params = False
    for line in (FIXTURES / "workflow_catalogue.txt").read_text("utf-8").splitlines():
        if re.match(r"^[a-z][a-z_]+$", line):
            current = line
            parsed[current] = {}
            in_params = False
        if "Parameters:" in line:
            in_params = True
            line = line.split("Parameters:", 1)[1]
        if in_params and current:
            for match in re.finditer(r"(\w+) \(([^,)]+)", line):
                parsed[current][match.group(1)] = match.group(2).strip()
            if line.rstrip().endswith(")") and not line.rstrip().endswith("),"):
                in_params = False
    return parsed


_TYPE_WORDS = {
    "string": "string",
    "integer": "integer",
    "list of strings": "array",
    "string or null": "string-or-null",
}


def test_the_request_schemas_match_the_catalogue_parameter_for_parameter() -> None:
    catalogue = _catalogue_parameters()
    schemas = json.loads(
        (FIXTURES / "workflow_request_schemas.json").read_text("utf-8")
    )["workflows"]
    assert sorted(schemas) == sorted(catalogue)
    for workflow, params in catalogue.items():
        schema = schemas[workflow]
        assert sorted(schema["properties"]) == sorted(params), workflow
        assert schema["additionalProperties"] is False
        for name, words in params.items():
            declared = schema["properties"][name]["type"]
            kind = "string-or-null" if isinstance(declared, list) else declared
            assert kind == _TYPE_WORDS[words], (workflow, name)
            nullable = words.endswith("or null")
            assert (name in schema["required"]) is (not nullable), (workflow, name)


def test_every_r3b_bundle_prompt_embeds_the_catalogue_the_schemas_describe() -> None:
    catalogue = (FIXTURES / "workflow_catalogue.txt").read_text("utf-8")
    for task_id in R3B_IDS:
        assert catalogue in _bundle(task_id).prompt


def test_the_held_workflow_of_every_bundle_is_in_the_catalogue() -> None:
    schemas = json.loads(
        (FIXTURES / "workflow_request_schemas.json").read_text("utf-8")
    )["workflows"]
    for task_id in R3B_IDS:
        assert _bundle(task_id).scorer_config["expected_workflow"] in schemas


def test_parse_reads_the_name_line_and_the_first_object() -> None:
    name, params, problem = parse_workflow_request('read_and_answer\n{"repo": "x"}\n')
    assert (name, params, problem) == ("read_and_answer", {"repo": "x"}, "")
    assert parse_workflow_request("Sure! I would run it.\n{}")[0] is None


# -- AC5: no numeric literal enters the scoring path -------------------------


def _workflow_request_functions() -> list[ast.FunctionDef]:
    tree = ast.parse(Path(workflow_request_score.__file__).read_text(encoding="utf-8"))
    return [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]


def test_ac5_the_check_finds_the_new_scorer_functions() -> None:
    """Positive control: an empty match would make the literal check vacuous."""
    names = {fn.name for fn in _workflow_request_functions()}
    assert names == {
        "parse_workflow_request",
        "score_workflow_request",
        "_workflow_request_patch_applies",
        "same_literal",
        "_text",
    }


def test_ac5_no_threshold_is_written_in_the_new_scorer() -> None:
    """The only numeric literals permitted are the zeros and ones of control flow."""
    permitted = {0, 1}
    offenders = [
        (fn.name, node.value)
        for fn in _workflow_request_functions()
        for node in ast.walk(fn)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
        and node.value not in permitted
    ]
    assert not offenders, f"thresholds leaked into the R3b scorer: {offenders}"


def test_ac5_the_module_level_constants_of_the_new_scorer_are_not_numeric() -> None:
    tree = ast.parse(Path(workflow_request_score.__file__).read_text(encoding="utf-8"))
    offenders = [
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
        and "WORKFLOW" in target.id
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, (int, float))
    ]
    assert not offenders
