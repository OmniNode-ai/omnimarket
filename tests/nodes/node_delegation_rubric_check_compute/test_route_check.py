# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Appendix A.0.2's 18 controls, plus pre-dispatch and malformed receipts."""

from copy import deepcopy
from importlib import import_module

import pytest
from pydantic import ValidationError

from omnimarket.adapters.codex.local_runtime_dispatch import _resolve_node_route
from omnimarket.adapters.codex.runtime_client import (
    CodexRuntimeRequestAdapter,
    _contract_dispatch_route,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.handlers.handler_delegation_route_check import (
    HandlerDelegationRouteCheck,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models.model_route_check_request import (
    ModelRouteCheckRequest,
)

pytestmark = pytest.mark.unit
REFERENCE = "cred_localinstall_gemini_" + "a" * 32
OTHER_REFERENCE = "cred_localinstall_gemini_" + "b" * 32


def receipt(route="L"):
    local = route == "L"
    pin = "local-coder" if local else "cloud-gemini-flash"
    backend = pin if local else "byok-gemini"
    terminal = {
        "model_name": "Qwen3.8-27B" if local else "gemini-2.5-flash-lite",
        "tenant_id": None if local else "walker-tenant",
        "secret_source": None if local else "store",
        "secret_ref": None if local else REFERENCE,
        "attempts": [
            {
                "acceptance_decision": "accept",
                "backend_id": backend,
                "substituted_from_backend_id": None if local else pin,
            }
        ],
        "metrics": {
            "input_tokens": 115,
            "output_tokens": 4,
            "cost_usd": 0.0 if local else 0.000238,
        },
    }
    return {
        "bus": "kafka" if local else "inmemory",
        "locus": "deployed-lane" if local else "in-process",
        "lane": "walk-lane" if local else None,
        "requested_backend_id": pin,
        "backend_pin_honoured": True,
        "backend_id": backend,
        "routing_tier": "local" if local else "cheap_cloud",
        "receipt": {"result_model": "ModelDelegateSkillResponse", "result": terminal},
    }


CASES = [
    ("deployed-glm-as-local", "L", "INVALID_ROUTE"),
    ("local-without-pin", "L", "INVALID_ROUTE"),
    ("local-pin-honoured", "L", "VALID"),
    ("local-house-tenant", "L", "INVALID_ROUTE"),
    ("local-cheap-cloud", "L", "INVALID_ROUTE"),
    ("gemini-house-key", "C", "INVALID_ROUTE"),
    ("customer-registration-matches", "C", "VALID"),
    ("customer-tenant-matches", "C", "VALID"),
    ("customer-registration-differs", "C", "INVALID_ROUTE"),
    ("customer-house-tenant", "C", "INVALID_ROUTE"),
    ("customer-other-provider-ref", "C", "INVALID_ROUTE"),
    ("customer-no-substitution", "C", "INVALID_ROUTE"),
    ("customer-ref-not-32-hex", "C", "INVALID_ROUTE"),
    ("customer-no-registration-ref", "C", "INVALID_ROUTE"),
    ("bare-terminal-payload", "L", "VALID"),
    ("bare-handler-result", "L", "VALID"),
    ("event-envelope-payload", "L", "VALID"),
    # AC4 deliberately replaces the appendix's INVALID_ROUTE for a dropped reply.
    ("dropped-reply", "L", "NO_TERMINAL"),
]


def request(case="local-pin-honoured", route="L"):
    doc = receipt(route)
    terminal = doc["receipt"]["result"]
    options = {"secret_ref": REFERENCE} if route == "C" else {"lane": "walk-lane"}
    if case == "deployed-glm-as-local":
        doc.update(backend_id="cloud-glm", routing_tier="cheap_cloud")
        terminal["model_name"] = "glm-4.5-flash"
        terminal["attempts"][0]["backend_id"] = "cloud-glm"
    elif case == "local-without-pin":
        doc.update(requested_backend_id=None, backend_pin_honoured=None)
    elif case in {"local-house-tenant", "customer-house-tenant"}:
        terminal["tenant_id"] = "omninode"
    elif case == "local-cheap-cloud":
        doc["routing_tier"] = "cheap_cloud"
    elif case == "gemini-house-key":
        doc["backend_id"] = "cloud-gemini-flash"
        terminal.update(secret_ref="llm.gemini.api_key", secret_source="house")
        terminal["attempts"][0].update(
            backend_id="cloud-gemini-flash", substituted_from_backend_id=None
        )
    elif case == "customer-tenant-matches":
        options["tenant"] = "walker-tenant"
    elif case == "customer-registration-differs":
        options["secret_ref"] = OTHER_REFERENCE
    elif case == "customer-other-provider-ref":
        terminal["secret_ref"] = "cred_localinstall_glm_" + "a" * 32
    elif case == "customer-no-substitution":
        terminal["attempts"][0]["substituted_from_backend_id"] = None
    elif case == "customer-ref-not-32-hex":
        terminal["secret_ref"] = "cred_localinstall_gemini_123"
    elif case == "customer-no-registration-ref":
        options.pop("secret_ref")
    elif case in {
        "bare-terminal-payload",
        "bare-handler-result",
        "event-envelope-payload",
    }:
        field = (
            "handler_result" if case == "bare-handler-result" else "terminal_payload"
        )
        carrier = (
            {"envelope_id": "event-id", "payload": terminal}
            if case == "event-envelope-payload"
            else terminal
        )
        doc["receipt"] = {"result_model": "RuntimeSummary", "result": {field: carrier}}
    elif case == "dropped-reply":
        doc["receipt"] = {"result_model": "RuntimeSummary", "result": {}}
    return ModelRouteCheckRequest(
        receipt=doc,
        route=route,
        pin="local-coder" if route == "L" else "cloud-gemini-flash",
        run_directory="/draws/run-123",
        **options,
    )


@pytest.mark.parametrize(
    ("case", "route", "expected"), CASES, ids=[c[0] for c in CASES]
)
def test_appendix_receipt_cases(case, route, expected):
    req = request(case, route)
    before = deepcopy(req.receipt)
    result = HandlerDelegationRouteCheck().handle(req)
    assert result.outcome == expected
    assert result.run_directory == req.run_directory
    assert req.receipt == before
    assert result.model_validate_json(result.model_dump_json()) == result
    if expected == "INVALID_ROUTE":
        assert result.failures
    elif expected == "NO_TERMINAL":
        assert result.draw_class == "REFUSED"
    else:
        assert not result.failures


@pytest.mark.parametrize(
    "reason",
    [
        "DelegateLocusRefusedError: no consumer",
        "DelegateDownstreamChainRefusedError: downstream refused",
    ],
)
def test_null_result_is_refused_with_cli_failure_reason(reason):
    req = request()
    doc = deepcopy(req.receipt)
    doc["receipt"]["result"] = None
    doc["failure_reason"] = reason
    result = HandlerDelegationRouteCheck().handle(
        req.model_copy(update={"receipt": doc})
    )
    assert result.outcome == "REFUSED"
    assert result.draw_class == "REFUSED"
    assert result.failure_reason == reason
    assert reason in result.failures[0]


@pytest.mark.parametrize(
    "doc",
    [
        {},
        {"receipt": None},
        {"receipt": {"result": {}}},
        {"receipt": {"result": "broken"}},
    ],
)
def test_unresolvable_terminal_is_no_terminal(doc):
    result = HandlerDelegationRouteCheck().handle(
        request().model_copy(update={"receipt": doc})
    )
    assert result.outcome == "NO_TERMINAL"
    assert result.draw_class == "REFUSED"


def test_route_operation_selects_its_handler_and_request_model():
    route = _resolve_node_route(
        "node_delegation_rubric_check_compute.delegation_route_check"
    )
    assert (
        getattr(import_module(route.handler_module), route.handler_class)
        is HandlerDelegationRouteCheck
    )
    assert (
        getattr(import_module(route.input_model_module), route.input_model_name)
        is ModelRouteCheckRequest
    )
    req = request()
    assert ModelRouteCheckRequest.model_validate(req.model_dump()) == req
    with pytest.raises(ValidationError):
        ModelRouteCheckRequest.model_validate({"task_class": "test"})


def test_unknown_operation_refuses_instead_of_selecting_first_handler():
    with pytest.raises(ValueError, match="operation"):
        _resolve_node_route("node_delegation_rubric_check_compute.no_such_operation")


def test_route_operation_runs_on_the_local_bus(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEX_LOCAL_RUNTIME_STATE_ROOT", str(tmp_path / "state"))
    result = CodexRuntimeRequestAdapter(requester="route-check-test").dispatch_sync(
        command_name="node_delegation_rubric_check_compute.delegation_route_check",
        payload=request().model_dump(mode="json"),
        runtime_selection="local",
        timeout_ms=5000,
    )
    assert result.ok
    assert result.output_payloads[0]["outcome"] == "VALID"
    assert result.output_payloads[0]["run_directory"] == "/draws/run-123"
    assert result.runtime_evidence.details["payload_model"].endswith(
        "ModelRouteCheckRequest"
    )


def test_deployed_binding_and_local_route_agree_on_the_model():
    binding = _contract_dispatch_route(
        "node_delegation_rubric_check_compute.delegation_route_check"
    )
    assert binding.payload_model.endswith("ModelRouteCheckRequest")


def test_rubric_operation_keeps_its_own_handler_and_model():
    explicit = _resolve_node_route(
        "node_delegation_rubric_check_compute.delegation_rubric_check"
    )
    legacy = _resolve_node_route("node_delegation_rubric_check_compute")
    assert explicit == legacy
    assert explicit.handler_class == "HandlerDelegationRubricCheck"
    assert explicit.input_model_name == "ModelRubricCheckRequest"
