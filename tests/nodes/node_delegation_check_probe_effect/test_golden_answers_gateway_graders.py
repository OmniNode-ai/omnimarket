# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gateway graders (gateway_checks.py) pass a good answer and refuse it with one clause removed (OMN-26642).

The answers in golden_answers_gateway.json are constructed from the declared response models of the real in-process workflow gateway
(never recorded from a live request) and replayed whatever the grader asks. The last groups send the probe's own requests,
built by its real HTTP client, to the real gateway app behind its real caller-identity resolvers, so a request the real
gateway refuses, or a credential sent in a header the resolver does not read, fails here. Nothing is skipped or marked as
expected to fail.
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
    AGREEMENT_FIELDS,
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
    Path(__file__).with_name("golden_answers_gateway.json").read_text(encoding="utf-8")
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
        return ModelGatewayObservation(
            status=v["status"], body=copy.deepcopy(v["body"])
        )

    def call(
        self, method: str, path: str, *, auth: str, body: dict[str, Any] | None = None
    ) -> ModelGatewayObservation:
        if path == "/health":
            return self._o("health")
        if method == "GET" and path == "/v1/workflows/contracts":
            return self._o(
                {"none": "absent_key", "invalid": "wrong_key"}.get(auth, "contracts")
            )
        if method == "POST":
            if auth == "invalid":
                return self._o("submit_invalid_key")
            payload = (body or {}).get("payload", {})
            if payload.get("source") not in ("claude-code", "codex", "external-client"):
                return self._o("submit_unknown_source")
            return self._o(
                "submit_dying"
                if payload.get("task_type") == DYING
                else "submit_healthy"
            )
        m = re.match(r"^/v1/workflows/([^/]+)/(status|receipt)", path)
        if m:
            wid, kind = m.groups()
            ids = self.a["ids"]
            if wid == ids["healthy"]:
                return self._o(f"{kind}_healthy")
            if wid == ids["dying"]:
                return self._o(f"{kind}_dying")
            return self._o(
                "malformed"
                if kind == "receipt" and "runner_identity" not in path
                else "wrong_tenant"
            )
        return ModelGatewayObservation(status=404, body={"code": "resource.not_found"})


def _grade(check: str, answers: dict[str, Any]) -> tuple[EnumCheckVerdict, list[str]]:
    clock = _Clock()
    reading = run_gateway_check(
        check,
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
    (
        "C12",
        "catalogue_unique_and_backs_delegation",
        _contract("workflow_type", "renamed"),
    ),
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
    (
        "C17",
        "gateway_refusals_typed",
        lambda a: a["absent_key"].__setitem__("body", {"detail": "Unauthorized"}),
    ),
    ("C28", "healthy_consumed", running),
    ("C28", "invalid_payload_observed", _body("status_dying", "status", "completed")),
    ("C29", "completed_under_tenant_key", running),
    ("C29", "receipt_names_provider_and_model", no_provider),
    (
        "C29",
        "no_house_credential_in_receipt",
        _body("receipt_healthy", "result_content", HOUSE),
    ),
    ("C29", "house_key_never_stands_in", _status("submit_invalid_key", 202)),
    ("C30", "plugin_source_accepted", _status("submit_healthy", 500)),
    ("C30", "completed_through_gateway", running),
    ("C30", "receipt_names_provider_and_model", no_provider),
    ("C30", "unknown_source_refused", _status("submit_unknown_source", 202)),
    ("C30", "unknown_source_refused", _status("submit_unknown_source", 422)),
    (
        "C30",
        "unknown_source_refused",
        _body(
            "submit_unknown_source", "errors", [{"field": "task_type", "message": "x"}]
        ),
    ),
    ("C30", "unknown_source_refused", _body("submit_unknown_source", "errors", [])),
    (
        "C16",
        "dead_run_typed_failure",
        _body("status_dying", "terminal_failure_code", "ONEX_OTHER_CODE"),
    ),
    (
        "C14",
        "gateway_unroutable_class_typed_failure",
        _body("status_dying", "terminal_failure_class", "OtherError"),
    ),
    (
        "C28",
        "invalid_payload_observed",
        _body("status_dying", "terminal_failure_code", "ONEX_OTHER_CODE"),
    ),
]


# A status body that disagrees with the receipt of the same run on a field both declared models carry (D4): the status model
# never carries route or provider, so the agreement is read on the fields it does carry.
DISAGREEMENTS = [
    (check, clause, field)
    for check, clause in (
        ("C14", "gateway_status_agrees_with_receipt"),
        ("C16", "status_agrees_with_receipt"),
    )
    for field in AGREEMENT_FIELDS
    if field != "status"
]


def _differs(field: str) -> Callable[[dict[str, Any]], None]:
    def edit(a: dict[str, Any]) -> None:
        value = a["status_healthy"]["body"][field]
        if isinstance(value, list):
            a["status_healthy"]["body"][field] = []
        elif isinstance(value, int):
            a["status_healthy"]["body"][field] = value + 1
        elif value is None:
            a["status_healthy"]["body"][field] = "not-in-the-receipt"
        else:
            a["status_healthy"]["body"][field] = f"{value}-other"

    return edit


@pytest.mark.parametrize(
    ("check", "clause", "field"),
    DISAGREEMENTS,
    ids=[f"{c}-{f}" for c, _, f in DISAGREEMENTS],
)
def test_a_status_that_disagrees_with_the_receipt_fails_and_names_the_agreement_clause(
    check: str, clause: str, field: str
) -> None:
    verdict, failed = _grade(check, _edit(_differs(field)))
    assert verdict is EnumCheckVerdict.FAIL
    assert clause in failed


@pytest.mark.parametrize(
    ("check", "clause"),
    [
        ("C14", "gateway_status_agrees_with_receipt"),
        ("C16", "status_agrees_with_receipt"),
    ],
)
def test_a_status_body_missing_from_the_run_fails_the_agreement_clause(
    check: str, clause: str
) -> None:
    # the declared status model always carries the terminal fields once the run is terminal
    verdict, failed = _grade(
        check, _edit(lambda a: a["status_healthy"]["body"].pop("terminal_model_used"))
    )
    assert verdict is EnumCheckVerdict.FAIL
    assert clause in failed


@pytest.mark.parametrize("check", GATEWAY_CHECKS)
def test_the_good_answer_grades_pass(check: str) -> None:
    verdict, failed = _grade(check, ANSWERS)
    assert verdict is EnumCheckVerdict.PASS, failed


@pytest.mark.parametrize(
    ("check", "clause", "edit"), REMOVALS, ids=[f"{c}-{k}" for c, k, _ in REMOVALS]
)
def test_a_clause_removed_alone_fails_and_names_that_clause(
    check: str, clause: str, edit: Callable[[dict[str, Any]], None]
) -> None:
    verdict, failed = _grade(check, _edit(edit))
    assert verdict is EnumCheckVerdict.FAIL
    assert clause in failed


def _clause_names(check: str) -> list[str]:
    clock = _Clock()
    reading = run_gateway_check(
        check,
        Replay(ANSWERS),
        SETTINGS,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )
    return [c.name for c in reading.clauses]


@pytest.mark.parametrize("check", GATEWAY_CHECKS)
def test_every_clause_of_every_grader_has_a_removal_that_fails_it_by_name(
    check: str,
) -> None:
    """A clause with no failing case is a clause that can never fail (a grader fault of the status_agrees class)."""
    covered = {clause for c, clause, _ in REMOVALS if c == check}
    covered |= {clause for c, clause, _ in DISAGREEMENTS if c == check}
    clauses = _clause_names(check)
    assert clauses, f"{check} graded no clause"
    assert set(clauses) - covered == set(), (
        f"{check} clauses with no removal case: {sorted(set(clauses) - covered)}"
    )


class _Everything(Replay):
    def __init__(
        self,
        answers: dict[str, Any],
        *,
        status: int | None,
        body: Any,
        error: str | None,
    ) -> None:
        super().__init__(answers)
        self._r = ModelGatewayObservation(status=status, body=body, error=error)

    def call(
        self, method: str, path: str, *, auth: str, body: dict[str, Any] | None = None
    ) -> ModelGatewayObservation:
        return self._r


class _NoCredential(Replay):
    def call(
        self, method: str, path: str, *, auth: str, body: dict[str, Any] | None = None
    ) -> ModelGatewayObservation:
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
    return handler.handle(
        ModelDelegationCheckRequest(correlation_id="g", check_id=check)
    ).verdict


@pytest.mark.parametrize("check", ["C11", "C12", "C15", "C16", "C28", "C29", "C30"])
def test_an_empty_answer_a_timeout_and_a_missing_credential_are_never_pass(
    check: str,
) -> None:
    assert (
        _handle(_Everything(ANSWERS, status=200, body=None, error=None), check)
        is EnumCheckVerdict.FAIL
    )
    assert (
        _handle(
            _Everything(ANSWERS, status=None, body=None, error="TimeoutError"), check
        )
        is EnumCheckVerdict.NOT_MEASURED
    )
    assert (
        _handle(_NoCredential(ANSWERS), check) is EnumCheckVerdict.PRECONDITION_ABSENT
    )


# ----- the probe's own requests, built by its real HTTP client and sent to the real in-process gateway ---------------------

KINDS = ["api_key", "jwt"]


def _real(kind: str) -> Any:
    from _golden_real_gateway import VALID_KEY, InProcessGateway, tenant_jwt

    return InProcessGateway(VALID_KEY if kind == "api_key" else tenant_jwt(), kind)


def _run(check: str, gateway: Any) -> Any:
    clock = _Clock()
    return run_gateway_check(
        check,
        gateway,
        SETTINGS,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("check", GATEWAY_CHECKS)
def test_a_good_answer_of_the_real_gateway_passes_every_check_under_either_credential_kind(
    check: str, kind: str
) -> None:
    reading = _run(check, _real(kind))
    assert reading.verdict() is EnumCheckVerdict.PASS, [
        (c.name, c.detail) for c in reading.clauses if not c.ok
    ]


@pytest.mark.parametrize("check", GATEWAY_CHECKS)
def test_every_submit_a_check_builds_names_a_uuid_correlation(check: str) -> None:
    gateway = _real("api_key")
    _run(check, gateway)
    sent = [s["body"] for s in gateway.sent if s["path"] == "/v1/workflows"]
    for body in sent:
        assert uuid.UUID(body["correlation_id"]).version == 4
    assert len({b["correlation_id"] for b in sent}) == len(sent)


def test_an_unknown_source_is_refused_400_naming_the_source_field_by_the_real_gateway() -> (
    None
):
    gateway = _real("api_key")
    observed, workflow_id = _session(gateway).submit("test", "not-a-source")
    assert workflow_id is None
    assert observed.status == 400
    assert [e["field"] for e in observed.body["errors"]] == ["source"]


def _session(gateway: Any) -> Any:
    from omnimarket.nodes.node_delegation_check_probe_effect.handlers.gateway_checks import (
        _Session,
    )

    return _Session(gateway, SETTINGS, "C30", lambda _s: None, lambda: 0.0)


# ----- the credential kinds, through the real CompositeCallerIdentity -------------------------------------------------------


def test_a_registered_onxk_key_in_x_api_key_is_accepted() -> None:
    gateway = _real("api_key")
    got = gateway.call("GET", "/v1/workflows/contracts", auth="tenant")
    assert got.status == 200
    assert gateway.seen_headers[-1]["x-api-key"].startswith("onxk_")
    assert "authorization" not in gateway.seen_headers[-1]


def test_a_jwt_shaped_value_in_x_api_key_is_refused_401_credential_invalid() -> None:
    from _golden_real_gateway import InProcessGateway, tenant_jwt

    # a row that says api_key while the tenant token is a JWT: the header the resolver reads for a key is the wrong one for it
    gateway = InProcessGateway(tenant_jwt(), "api_key")
    got = gateway.call("GET", "/v1/workflows/contracts", auth="tenant")
    assert gateway.seen_headers[-1]["x-api-key"].count(".") == 2
    assert got.status == 401
    assert got.body["code"] == "auth.credential.invalid"


def test_a_bearer_jwt_is_accepted_where_the_resolver_takes_one() -> None:
    gateway = _real("jwt")
    got = gateway.call("GET", "/v1/workflows/contracts", auth="tenant")
    assert got.status == 200
    assert gateway.seen_headers[-1]["authorization"].startswith("Bearer ")
    assert "x-api-key" not in gateway.seen_headers[-1]


def test_a_bearer_signed_by_another_issuer_is_refused_401_credential_invalid() -> None:
    from _golden_real_gateway import OTHER_SIGNER, InProcessGateway, tenant_jwt

    got = InProcessGateway(tenant_jwt(key=OTHER_SIGNER), "jwt").call(
        "GET", "/v1/workflows/contracts", auth="tenant"
    )
    assert got.status == 401
    assert got.body["code"] == "auth.credential.invalid"


def test_a_registered_key_sent_as_a_bearer_is_refused_never_retried_as_a_key() -> None:
    from _golden_real_gateway import VALID_KEY, InProcessGateway

    got = InProcessGateway(VALID_KEY, "jwt").call(
        "GET", "/v1/workflows/contracts", auth="tenant"
    )
    assert got.status == 401
    assert got.body["code"] == "auth.credential.invalid"


@pytest.mark.parametrize("kind", KINDS)
def test_the_credential_no_tenant_owns_is_refused_401_credential_invalid_under_either_kind(
    kind: str,
) -> None:
    got = _real(kind).call("GET", "/v1/workflows/contracts", auth="invalid")
    assert got.status == 401
    assert got.body["code"] == "auth.credential.invalid"


@pytest.mark.parametrize(
    "check", ["C11", "C12", "C13", "C14", "C15", "C16", "C17", "C28", "C29", "C30"]
)
def test_a_jwt_in_the_api_key_header_fails_the_check_where_the_right_header_passes(
    check: str,
) -> None:
    from _golden_real_gateway import InProcessGateway, tenant_jwt

    wrong_header = _run(check, InProcessGateway(tenant_jwt(), "api_key"))
    assert wrong_header.verdict() is EnumCheckVerdict.FAIL
    assert (
        _run(check, InProcessGateway(tenant_jwt(), "jwt")).verdict()
        is EnumCheckVerdict.PASS
    )


# ----- a real wrong answer from the real gateway -------------------------------------------------------------------------


def _tampered(kind_of_answer: str, field: str, value: Any) -> Any:
    from _golden_real_gateway import VALID_KEY, InProcessGateway

    class Tampered(InProcessGateway):
        def call(
            self,
            method: str,
            path: str,
            *,
            auth: str,
            body: dict[str, Any] | None = None,
        ) -> ModelGatewayObservation:
            got = super().call(method, path, auth=auth, body=body)
            if (
                got.status == 200
                and isinstance(got.body, dict)
                and path.split("?")[0].endswith(f"/{kind_of_answer}")
                and got.body.get("status") == "completed"
            ):
                got = ModelGatewayObservation(
                    status=200, body={**got.body, field: value}
                )
            return got

    return Tampered(VALID_KEY, "api_key")


@pytest.mark.parametrize("answer", ["status", "receipt"])
@pytest.mark.parametrize(
    ("check", "clause"),
    [
        ("C14", "gateway_status_agrees_with_receipt"),
        ("C16", "status_agrees_with_receipt"),
    ],
)
def test_a_real_run_whose_status_and_receipt_name_different_models_fails_the_agreement_clause(
    check: str, clause: str, answer: str
) -> None:
    reading = _run(check, _tampered(answer, "terminal_model_used", "another-model"))
    assert reading.verdict() is EnumCheckVerdict.FAIL
    assert clause in [c.name for c in reading.clauses if not c.ok]
