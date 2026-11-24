# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gateway graders (gateway_checks.py) pass a recorded good answer and refuse it with one clause removed (OMN-26652).

Meant for omnimarket#6765, beside tests/nodes/node_delegation_check_probe_effect/golden_answers.json (from the golden-answers
pull request). The answers are replayed whatever the grader asks; the last group sends the probe's own requests to the real
in-process gateway app, so a request the real gateway refuses fails here. No test is skipped or marked as expected to fail.
"""

from __future__ import annotations

import copy
import json
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.overlay_documents.model_delegation_check_responders_overlay import (
    ModelDelegationCheckResponder,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_delegation_check import (
    AbsentCustomerMachine,
    InMemoryCheckPublisher,
    InMemoryResponderReader,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.gateway_checks import (
    GATEWAY_CHECKS,
    GatewayRunSettings,
    run_gateway_check,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.handler_delegation_check_probe import (
    HandlerDelegationCheckProbe,
    contract_topics,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    EnumCheckVerdict,
    ModelDelegationCheckRequest,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_gateway_check import (
    ModelGatewayObservation,
)
from omnimarket.nodes.node_delegation_check_probe_effect.protocols.protocol_delegation_check import (
    CollectorAbsentError,
)

pytestmark = pytest.mark.unit
ANSWERS: dict[str, Any] = json.loads(
    Path(__file__).with_name("golden_answers.json").read_text(encoding="utf-8")
)["gateway"]
DYING = "omn19181_not_a_task_class"
SETTINGS = GatewayRunSettings(run_budget_s=5.0, poll_interval_s=0.0)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, _s: float) -> None:
        self.now += 1.0

    def monotonic(self) -> float:
        return self.now


class Replay:
    """Serves the stored answers to whatever a grader asks; the answer is what is graded, never the request."""

    gateway_url = "http://gateway.test.example:4100"
    tenant_slug = "tenant-a"

    def __init__(self, answers: dict[str, Any]) -> None:
        self.a = answers

    def _o(self, key: str) -> ModelGatewayObservation:
        v = self.a["answers"][key]
        return ModelGatewayObservation(status=v["status"], body=copy.deepcopy(v["body"]))

    def call(
        self, method: str, path: str, *, auth: str, body: dict[str, Any] | None = None
    ) -> ModelGatewayObservation:
        if path == "/health":
            return self._o("health")
        if method == "GET" and path == "/v1/workflows/contracts":
            return self._o({"none": "absent_key", "invalid": "wrong_key"}.get(auth, "contracts"))
        if method == "POST":
            if auth == "invalid":
                return self._o("submit_invalid_key")
            payload = (body or {}).get("payload", {})
            if payload.get("source") not in ("claude-code", "codex", "external-client"):
                return self._o("submit_unknown_source")
            return self._o("submit_dying" if payload.get("task_type") == DYING else "submit_healthy")
        m = re.match(r"^/v1/workflows/([^/]+)/(status|receipt)", path)
        if m:
            wid, kind = m.groups()
            ids = self.a["ids"]
            if wid == ids["healthy"]:
                return self._o(f"{kind}_healthy")
            if wid == ids["dying"]:
                return self._o(f"{kind}_dying")
            return self._o("malformed" if kind == "receipt" and "runner_identity" not in path else "wrong_tenant")
        return ModelGatewayObservation(status=404, body={"code": "resource.not_found"})


def _grade(check: str, answers: dict[str, Any]) -> tuple[EnumCheckVerdict, list[str]]:
    clock = _Clock()
    reading = run_gateway_check(
        check,  # type: ignore[arg-type]
        Replay(answers),
        SETTINGS,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    return reading.verdict(), [c.name for c in reading.clauses if not c.ok]


def _edit(fn: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    answers = copy.deepcopy(ANSWERS)
    fn(answers["answers"])
    return answers


def _status(key: str, value: int) -> Callable[[dict[str, Any]], None]:
    return lambda a: a[key].__setitem__("status", value)


def _body(key: str, field: str, value: Any) -> Callable[[dict[str, Any]], None]:
    return lambda a: a[key]["body"].__setitem__(field, value)


def _contract(field: str, value: Any) -> Callable[[dict[str, Any]], None]:
    return lambda a: a["contracts"]["body"]["contracts"][0].__setitem__(field, value)


running = _body("status_healthy", "status", "running")
no_provider = _body("receipt_healthy", "provider", None)
no_route = _body("receipt_healthy", "route", None)
bad_code = _body("receipt_dying", "terminal_failure_code", "bad code")
HOUSE = "llm.openrouter.house"

REMOVALS: list[tuple[str, str, Callable[[dict[str, Any]], None]]] = [
    ("C11", "positive_control", _status("health", 503)),
    ("C11", "wrong_key", _status("wrong_key", 200)),
    ("C11", "absent_key", _status("absent_key", 200)),
    ("C11", "wrong_tenant", _status("wrong_tenant", 200)),
    ("C11", "malformed", _status("malformed", 200)),
    ("C12", "catalogue_served", _status("contracts", 206)),
    ("C12", "catalogue_unique_and_backs_delegation", _contract("workflow_type", "renamed")),
    ("C12", "no_house_entry", _contract("credential_ref", HOUSE)),
    ("C13", "gateway_delegation_completes", running),
    ("C13", "gateway_receipt_names_route_provider_model", no_route),
    ("C14", "gateway_routed_run_completes", running),
    ("C14", "gateway_unroutable_class_typed_failure", bad_code),
    ("C15", "submitted", _status("submit_healthy", 500)),
    ("C15", "completed_within_budget", running),
    ("C15", "receipt_names_route_provider_model", no_provider),
    ("C16", "healthy_completed", running),
    ("C16", "receipt_identity_present", no_provider),
    ("C16", "rules_evaluated", _body("receipt_healthy", "rule_evaluations", [])),
    ("C16", "dead_run_typed_failure", bad_code),
    ("C17", "gateway_surface_answers", _status("health", 503)),
    ("C17", "gateway_refusals_typed", lambda a: a["absent_key"].__setitem__("body", {"detail": "Unauthorized"})),
    ("C28", "healthy_consumed", running),
    ("C28", "invalid_payload_observed", _body("status_dying", "status", "completed")),
    ("C29", "completed_under_tenant_key", running),
    ("C29", "receipt_names_provider_and_model", no_provider),
    ("C29", "no_house_credential_in_receipt", _body("receipt_healthy", "result_content", HOUSE)),
    ("C29", "house_key_never_stands_in", _status("submit_invalid_key", 202)),
    ("C30", "plugin_source_accepted", _status("submit_healthy", 500)),
    ("C30", "completed_through_gateway", running),
    ("C30", "receipt_names_provider_and_model", no_provider),
    ("C30", "unknown_source_refused", _status("submit_unknown_source", 202)),
]


@pytest.mark.parametrize("check", GATEWAY_CHECKS)
def test_the_good_answer_grades_pass(check: str) -> None:
    verdict, failed = _grade(check, ANSWERS)
    assert verdict is EnumCheckVerdict.PASS, failed


@pytest.mark.parametrize(("check", "clause", "edit"), REMOVALS, ids=[f"{c}-{k}" for c, k, _ in REMOVALS])
def test_a_clause_removed_alone_fails_and_names_that_clause(
    check: str, clause: str, edit: Callable[[dict[str, Any]], None]
) -> None:
    verdict, failed = _grade(check, _edit(edit))
    assert verdict is EnumCheckVerdict.FAIL
    assert clause in failed


class _Everything(Replay):
    def __init__(self, answers: dict[str, Any], *, status: int | None, body: Any, error: str | None) -> None:
        super().__init__(answers)
        self._r = ModelGatewayObservation(status=status, body=body, error=error)

    def call(self, method: str, path: str, *, auth: str, body: dict[str, Any] | None = None) -> ModelGatewayObservation:
        return self._r


class _NoCredential(Replay):
    def call(self, method: str, path: str, *, auth: str, body: dict[str, Any] | None = None) -> ModelGatewayObservation:
        if auth == "tenant":
            raise CollectorAbsentError("tenant_credential", "no token")
        return super().call(method, path, auth=auth, body=body)


def _handle(gateway: Replay, check: str) -> EnumCheckVerdict:
    clock = _Clock()
    handler = HandlerDelegationCheckProbe(
        responders=(ModelDelegationCheckResponder(tier="openrouter"),),
        reader=InMemoryResponderReader({}),
        machine=AbsentCustomerMachine("no machine in this test"),
        publisher=InMemoryCheckPublisher(),
        topic=contract_topics()[0],
        clock=lambda: datetime(2026, 11, 24, 15, 0, tzinfo=UTC),
        gateway=gateway,
        gateway_settings=SETTINGS,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    return handler.handle(ModelDelegationCheckRequest(correlation_id="g", check_id=check)).verdict  # type: ignore[arg-type]


@pytest.mark.parametrize("check", ["C11", "C12", "C15", "C16", "C28", "C29", "C30"])
def test_an_empty_answer_a_timeout_and_a_missing_credential_are_never_pass(check: str) -> None:
    assert _handle(_Everything(ANSWERS, status=200, body=None, error=None), check) is EnumCheckVerdict.FAIL
    assert _handle(_Everything(ANSWERS, status=None, body=None, error="TimeoutError"), check) is EnumCheckVerdict.NOT_MEASURED
    assert _handle(_NoCredential(ANSWERS), check) is EnumCheckVerdict.PRECONDITION_ABSENT


# ----- the probe's own requests, sent to the real in-process gateway ---------------------------------------------------


def _real(credential: str):  # type: ignore[no-untyped-def]
    from _golden_real_gateway import InProcessGateway

    return InProcessGateway(credential)


@pytest.mark.parametrize("check", GATEWAY_CHECKS)
def test_the_requests_a_check_builds_are_accepted_by_the_real_gateway(check: str) -> None:
    from _golden_real_gateway import VALID_KEY

    clock = _Clock()
    reading = run_gateway_check(
        check,  # type: ignore[arg-type]
        _real(VALID_KEY),
        SETTINGS,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    assert reading.verdict() is EnumCheckVerdict.PASS, [(c.name, c.detail) for c in reading.clauses if not c.ok]


def test_the_submit_a_check_builds_names_a_uuid_correlation() -> None:
    from _golden_real_gateway import VALID_KEY

    gateway = _real(VALID_KEY)
    clock = _Clock()
    run_gateway_check("C15", gateway, SETTINGS, sleep=clock.sleep, monotonic=clock.monotonic)  # type: ignore[arg-type]
    sent = gateway.sent[0]["body"]
    uuid.UUID(str(sent.get("correlation_id", uuid.uuid4())))
