# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Documentation controls, parse-only answers, and operation dispatch."""

import json
from importlib import import_module
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route
from omnimarket.adapters.codex.runtime_client import CodexRuntimeRequestAdapter
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.handler_delegation_doc_check import (
    HandlerDelegationDocCheck,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.handler_delegation_route_check import (
    HandlerDelegationRouteCheck,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.handler_delegation_rubric_check import (
    HandlerDelegationRubricCheck,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_doc_check_request import (
    ModelDocCheckRequest,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_doc_check_verdict import (
    ModelDocCheckVerdict,
)

pytestmark = pytest.mark.unit
FIXTURES = (
    Path(__file__).parents[2] / "fixtures" / "delegation" / "omn20218" / "doc_check"
)
REFERENCE_SOURCE = (FIXTURES / "retry_delay_source.txt").read_text(encoding="utf-8")
REFERENCE_ANSWER = (FIXTURES / "retry_delay_reference_answer.txt").read_text(
    encoding="utf-8"
)
CONTROLS = json.loads((FIXTURES / "controls.json").read_text(encoding="utf-8"))


def request(answer_text=REFERENCE_ANSWER):
    return ModelDocCheckRequest(
        answer_text=answer_text,
        reference_source=REFERENCE_SOURCE,
        run_directory="/draws/run-123",
    )


def control_answer(control):
    answer = REFERENCE_ANSWER
    edits = (
        control["edits"] if "edits" in control else [(control["old"], control["new"])]
    )
    for old, new in edits:
        if old:
            assert answer.count(old) == 1, control["id"]
            answer = answer.replace(old, new, 1)
    return answer


def test_doc_operation_selects_its_handler_and_request_model():
    route = _resolve_node_route(
        "node_delegation_rubric_check_compute.delegation_doc_check"
    )
    assert (
        getattr(import_module(route.handler_module), route.handler_class)
        is HandlerDelegationDocCheck
    )
    assert (
        getattr(import_module(route.input_model_module), route.input_model_name)
        is ModelDocCheckRequest
    )
    req = request()
    assert ModelDocCheckRequest.model_validate(req.model_dump()) == req
    with pytest.raises(ValidationError):
        ModelDocCheckRequest.model_validate({"task_class": "test"})


@pytest.mark.parametrize(
    ("operation", "handler", "model_name"),
    [
        (
            "delegation_rubric_check",
            HandlerDelegationRubricCheck,
            "ModelRubricCheckRequest",
        ),
        (
            "delegation_route_check",
            HandlerDelegationRouteCheck,
            "ModelRouteCheckRequest",
        ),
    ],
)
def test_other_operations_keep_their_handlers(operation, handler, model_name):
    route = _resolve_node_route(f"node_delegation_rubric_check_compute.{operation}")
    assert getattr(import_module(route.handler_module), route.handler_class) is handler
    assert route.input_model_name == model_name


@pytest.mark.parametrize("control", CONTROLS, ids=[c["id"] for c in CONTROLS])
def test_appendix_doc_controls(control):
    result = HandlerDelegationDocCheck().handle(request(control_answer(control)))
    assert result.outcome.value == control["expect"], result.failures


def test_twenty_eight_controls_loaded():
    assert len(CONTROLS) == 28
    assert sum(c["expect"] == "PASS" for c in CONTROLS) == 9
    assert sum(c["expect"] == "FAIL" for c in CONTROLS) == 19


@pytest.mark.parametrize(
    ("control_id", "failure"),
    [
        ("F1", "wrong default for base"),
        ("F14", "doctest"),
        ("F16", "the code was changed"),
        ("F17", "invented identifier in backticks: max_delay"),
    ],
)
def test_failures_name_what_was_wrong(control_id, failure):
    control = next(c for c in CONTROLS if c["id"].split()[0] == control_id)
    result = HandlerDelegationDocCheck().handle(request(control_answer(control)))
    assert result.outcome.value == "FAIL"
    assert any(failure in entry for entry in result.failures), result.failures


def test_verdict_carries_run_directory():
    req = request()
    result = HandlerDelegationDocCheck().handle(req)
    assert result.run_directory == req.run_directory


@pytest.mark.timeout(5)
@pytest.mark.parametrize(
    "example",
    ["__import__('os').getcwd()", "retry_delay(10**9)", None],
    ids=["doctest-import", "doctest-large-exponent", "module-system-exit"],
)
def test_answer_is_never_executed(example):
    if example is None:
        answer = "raise SystemExit\n\n" + REFERENCE_ANSWER
    else:
        answer = REFERENCE_ANSWER.replace(
            "    Examples:\n", f"    Examples:\n        >>> {example}\n", 1
        )
    result = HandlerDelegationDocCheck().handle(request(answer))
    assert isinstance(result, ModelDocCheckVerdict)
    if example is None:
        assert result.outcome.value in {"PASS", "FAIL"}
    else:
        assert result.outcome.value == "FAIL"
        assert any("doctest" in entry for entry in result.failures), result.failures


@pytest.mark.parametrize(
    ("answer", "failure"),
    [
        (f"```python\n{REFERENCE_ANSWER}\n```", None),
        (
            f"```python\n{REFERENCE_ANSWER}\n```\n\n```python\nx = 1\n```",
            "one fenced code block",
        ),
        ("This is not Python!", "does not parse"),
        (
            REFERENCE_ANSWER.replace("def retry_delay(", "def another_function(", 1),
            "does not define retry_delay",
        ),
    ],
    ids=["python-fence", "two-fences", "not-python", "different-function"],
)
def test_fenced_answer_is_unwrapped(answer, failure):
    result = HandlerDelegationDocCheck().handle(request(answer))
    if failure is None:
        assert result.outcome.value == "PASS", result.failures
    else:
        assert result.outcome.value == "FAIL"
        assert any(failure in entry for entry in result.failures), result.failures


@pytest.mark.parametrize(
    "overrides",
    [
        {"reference_source": "This is not Python!"},
        {"reference_source": "value = 1"},
        {"reference_source": "def another_function():\n    return 1\n"},
        {"reference_source": REFERENCE_SOURCE + "\ndef other():\n    return 1\n"},
        {"reference_source": "value = 1\n" + REFERENCE_SOURCE},
        {"unexpected": "extra field"},
    ],
    ids=[
        "not-python",
        "not-a-function",
        "wrong-function-name",
        "two-functions",
        "extra-module-code",
        "extra-field",
    ],
)
def test_request_rejects_a_bad_reference(overrides):
    payload = {**request().model_dump(), **overrides}
    with pytest.raises(ValidationError):
        ModelDocCheckRequest.model_validate(payload)


def test_doc_operation_runs_on_the_local_bus(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEX_LOCAL_RUNTIME_STATE_ROOT", str(tmp_path / "state"))
    req = request()
    result = CodexRuntimeRequestAdapter(requester="doc-check-test").dispatch_sync(
        command_name="node_delegation_rubric_check_compute.delegation_doc_check",
        payload=req.model_dump(mode="json"),
        runtime_selection="local",
        timeout_ms=5000,
    )
    assert result.ok
    assert result.output_payloads[0]["outcome"] == "PASS"
    assert result.output_payloads[0]["run_directory"] == req.run_directory
    assert result.runtime_evidence.details["payload_model"].endswith(
        "ModelDocCheckRequest"
    )
