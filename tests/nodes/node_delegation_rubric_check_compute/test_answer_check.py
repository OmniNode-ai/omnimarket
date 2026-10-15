# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Appendix A.4's reasoning-case answer checker: its 19 labelled responses, and routing."""

from importlib import import_module

import pytest
from pydantic import ValidationError

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route
from omnimarket.adapters.codex.runtime_client import (
    CodexRuntimeRequestAdapter,
    _contract_dispatch_route,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.handler_delegation_answer_check import (
    HandlerDelegationAnswerCheck,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_answer_check_request import (
    ModelAnswerCheckRequest,
)

pytestmark = pytest.mark.unit

EXPECTED = [
    "PR-101: MERGE",
    "PR-102: BLOCKED",
    "PR-103: WAIT",
    "PR-104: MERGE",
    "PR-105: NEEDS_REVIEW",
    "PR-106: CONFLICT",
    "PR-107: BLOCKED",
    "PR-108: MERGE",
    "PR-109: WAIT",
    "PR-110: NEEDS_REVIEW",
    "PR-111: BLOCKED",
    "PR-112: NEEDS_REVIEW",
]
REASON = (
    "Step 1. PR-101: BLOCKED is what a hasty read gives, but rule 1 needs a failure;"
    " there is none.\n"
    "Step 2. PR-107 has a failure, so BLOCKED beats its pending lint.\n"
)


def swap(lines, n, bucket):
    return [
        f"PR-{n}: {bucket}" if line.startswith(f"PR-{n}:") else line for line in lines
    ]


def joined(lines):
    return "\n".join(lines) + "\n"


# name: (response text, expected verdict) -- the generator of appendix A.4, case for case
CASES = {
    "good_plain": (joined(EXPECTED), "PASS"),
    "good_with_reasoning_that_contradicts_itself": (
        REASON + "\n" + joined(EXPECTED),
        "PASS",
    ),
    "good_after_answer_marker": (REASON + "\n### ANSWER\n" + joined(EXPECTED), "PASS"),
    "good_marker_then_steps_then_lines": (
        "### ANSWER\n1. rule 1 first.\n2. rule 2 next.\n\n" + joined(EXPECTED),
        "PASS",
    ),
    "good_fenced": (REASON + "\n```\n" + joined(EXPECTED) + "```\n", "PASS"),
    "good_crlf_and_trailing_spaces": (
        "\r\n".join(line + "  " for line in EXPECTED) + "\r\n",
        "PASS",
    ),
    "bad_one_wrong_bucket": (joined(swap(EXPECTED, 110, "WAIT")), "FAIL"),
    "bad_missing_id": (joined(EXPECTED[:5] + EXPECTED[6:]), "FAIL"),
    "bad_conflicting_duplicate": (joined([*EXPECTED, "PR-105: MERGE"]), "FAIL"),
    "bad_extra_line_before": (joined(["PR-100: MERGE", *EXPECTED]), "FAIL"),
    "bad_prose_after_the_twelve": (
        REASON + "\n".join(EXPECTED) + "\nThat is my classification.\n",
        "FAIL",
    ),
    "good_no_blank_line_before_the_lines": (
        REASON.rstrip() + "\nFinal answer:\n" + joined(EXPECTED),
        "PASS",
    ),
    "bad_six_two_column_lines": (
        joined([f"{EXPECTED[i]:<22}{EXPECTED[i + 6]}" for i in range(6)]),
        "FAIL",
    ),
    "bad_wrong_order": (joined(EXPECTED[6:] + EXPECTED[:6]), "FAIL"),
    "bad_lowercase_bucket": (
        joined([line.replace("MERGE", "merge") for line in EXPECTED]),
        "FAIL",
    ),
    "bad_decorated_lines": (
        joined(["- **" + line + "**" for line in EXPECTED]),
        "FAIL",
    ),
    "bad_second_block_wrong": (
        joined(EXPECTED) + "\n" + joined(swap(EXPECTED, 101, "WAIT")),
        "FAIL",
    ),
    "bad_right_lines_inside_reasoning_only": (
        joined(EXPECTED) + "The rest follow the same rules.\n",
        "FAIL",
    ),
    "bad_empty": ("", "FAIL"),
}


def request(response="", expected=None, run_directory="/draws/rs-l-1"):
    return ModelAnswerCheckRequest(
        expected=joined(EXPECTED) if expected is None else expected,
        response=response,
        run_directory=run_directory,
    )


def test_the_appendix_carries_nineteen_labelled_responses():
    assert len(CASES) == 19
    assert sum(1 for _, verdict in CASES.values() if verdict == "PASS") == 7


@pytest.mark.parametrize("name", list(CASES))
def test_appendix_response_cases(name):
    response, expected = CASES[name]
    verdict = HandlerDelegationAnswerCheck().handle(request(response))
    assert verdict.outcome == expected, verdict.failures
    assert verdict.run_directory == "/draws/rs-l-1"
    assert bool(verdict.failures) == (expected == "FAIL")
    assert verdict.model_validate_json(verdict.model_dump_json()) == verdict


def test_pass_reports_the_twelve_lines_it_graded():
    verdict = HandlerDelegationAnswerCheck().handle(
        request(REASON + "### ANSWER\n" + joined(EXPECTED))
    )
    assert verdict.final_block == tuple(EXPECTED)


@pytest.mark.parametrize(
    ("name", "needle"),
    [
        ("bad_one_wrong_bucket", "expected 'PR-110: NEEDS_REVIEW', got 'PR-110: WAIT'"),
        ("bad_missing_id", "final block has 11 answer lines, not 12"),
        ("bad_extra_line_before", "final block has 13 answer lines, not 12"),
        ("bad_prose_after_the_twelve", "the response does not end with answer lines"),
        ("bad_empty", "empty response"),
    ],
)
def test_failures_name_what_was_wrong(name, needle):
    verdict = HandlerDelegationAnswerCheck().handle(request(CASES[name][0]))
    assert any(needle in failure for failure in verdict.failures), verdict.failures


def test_duplicate_ids_are_named():
    verdict = HandlerDelegationAnswerCheck().handle(
        request(CASES["bad_conflicting_duplicate"][0])
    )
    assert any("duplicate ids in the final block: 105" in f for f in verdict.failures)


def test_an_expected_file_that_is_not_twelve_lines_cannot_pass_silently():
    verdict = HandlerDelegationAnswerCheck().handle(
        request(joined(EXPECTED), expected=joined(EXPECTED[:11]))
    )
    assert verdict.outcome == "FAIL"
    assert verdict.failures


def test_request_refuses_unknown_fields_and_a_foreign_operation():
    with pytest.raises(ValidationError):
        ModelAnswerCheckRequest.model_validate(
            {"expected": "x", "response": "y", "run_directory": "/d", "extra": 1}
        )
    with pytest.raises(ValidationError):
        ModelAnswerCheckRequest.model_validate(
            {
                "operation": "delegation_route_check",
                "expected": "x",
                "response": "y",
                "run_directory": "/d",
            }
        )
    with pytest.raises(ValidationError):
        ModelAnswerCheckRequest.model_validate({"task_class": "test"})


def test_answer_operation_selects_its_handler_and_request_model():
    route = _resolve_node_route(
        "node_delegation_rubric_check_compute.delegation_answer_check"
    )
    assert (
        getattr(import_module(route.handler_module), route.handler_class)
        is HandlerDelegationAnswerCheck
    )
    assert (
        getattr(import_module(route.input_model_module), route.input_model_name)
        is ModelAnswerCheckRequest
    )
    assert _contract_dispatch_route(
        "node_delegation_rubric_check_compute.delegation_answer_check"
    ).payload_model.endswith("ModelAnswerCheckRequest")


def test_other_operations_keep_their_handlers():
    rubric = _resolve_node_route(
        "node_delegation_rubric_check_compute.delegation_rubric_check"
    )
    assert rubric.handler_class == "HandlerDelegationRubricCheck"
    route = _resolve_node_route(
        "node_delegation_rubric_check_compute.delegation_route_check"
    )
    assert route.handler_class == "HandlerDelegationRouteCheck"


def test_answer_operation_runs_on_the_local_bus(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEX_LOCAL_RUNTIME_STATE_ROOT", str(tmp_path / "state"))
    result = CodexRuntimeRequestAdapter(requester="answer-check-test").dispatch_sync(
        command_name="node_delegation_rubric_check_compute.delegation_answer_check",
        payload=request(joined(EXPECTED)).model_dump(mode="json"),
        runtime_selection="local",
        timeout_ms=5000,
    )
    assert result.ok
    assert result.output_payloads[0]["outcome"] == "PASS"
    assert result.output_payloads[0]["run_directory"] == "/draws/rs-l-1"
    assert result.runtime_evidence.details["payload_model"].endswith(
        "ModelAnswerCheckRequest"
    )
