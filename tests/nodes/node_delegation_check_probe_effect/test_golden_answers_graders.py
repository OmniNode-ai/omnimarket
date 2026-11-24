# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each delegation check's grader passes a good answer and refuses it with one clause removed (OMN-26652).

The good answers live in ``golden_answers.json`` with their source named on every entry: a recorded answer carries its run id
and capture time, an answer built by hand says ``constructed from the declared response models``. The customer-machine
graders (C13, C14, C17) are fed the observation models; the gateway answers are checked against the gateway's own declared
status, receipt and acknowledgement models, so a constructed answer cannot drift from what the real gateway sends. The
credential case runs the gateway's real caller-identity resolver in process: it takes ``x-api-key`` only as a registered
``onxk_`` key and refuses anything else, a JWT included.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from _gateway_fake import FakeClock, FakeGateway

from omnimarket.models.overlay_documents.model_delegation_check_responders_overlay import (
    ModelDelegationCheckResponder,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_delegation_check import (
    AbsentCustomerMachine,
    InMemoryCheckPublisher,
    InMemoryCustomerMachine,
    InMemoryResponderReader,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.gateway_checks import (
    GatewayRunSettings,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.grade_delegation_checks import (
    grade_c13,
    grade_c14,
    grade_c17,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.handler_delegation_check_probe import (
    HandlerDelegationCheckProbe,
    contract_topics,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    CheckId,
    EnumCheckVerdict,
    ModelCheckClause,
    ModelDelegateObservation,
    ModelDelegationCheckReading,
    ModelDelegationCheckRequest,
    ModelRoutingCollection,
    ModelSurfaceObservation,
)
from omnimarket.nodes.node_delegation_check_probe_effect.protocols.protocol_delegation_check import (
    CollectorAbsentError,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.caller_identity import (
    CompositeCallerIdentity,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.http_app import create_app
from omnimarket.nodes.node_workflow_gateway_effect.handlers.identity_api_key_postgres import (
    PostgresApiKeyTenantIdentity,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_envelope import (
    ModelWorkflowAck,
    ModelWorkflowReceipt,
    ModelWorkflowStatus,
)
from tests.nodes.node_workflow_gateway_effect.fakes import TENANT_A, build_gateway

pytestmark = pytest.mark.unit

GOLDEN = json.loads(
    (Path(__file__).with_name("golden_answers.json")).read_text(encoding="utf-8")
)
TEN_CHECKS = ("C11", "C12", "C13", "C14", "C15", "C16", "C17", "C28", "C29", "C30")
RESPONDER = GOLDEN["responder_base"]
SERVED = GOLDEN["served_model"]
SOURCES = (
    "recorded status and code",
    "constructed from the declared response models",
)


def _delegation(**over: Any) -> ModelDelegateObservation:
    return ModelDelegateObservation.model_validate(
        {**GOLDEN["machine"]["delegation"], **over}
    )


def _routing(**over: Any) -> ModelRoutingCollection:
    return ModelRoutingCollection.model_validate(
        {**GOLDEN["machine"]["routing"], **over}
    )


def _surface(**over: Any) -> ModelSurfaceObservation:
    return ModelSurfaceObservation.model_validate(
        {**GOLDEN["machine"]["surface"], **over}
    )


def _c13(**over: Any) -> tuple[ModelCheckClause, ...]:
    return grade_c13(_delegation(**over), responder_base=RESPONDER, served_model=SERVED)


def _c14(
    delegation: dict[str, Any] | None = None, routing: dict[str, Any] | None = None
) -> tuple[ModelCheckClause, ...]:
    return grade_c14(_delegation(**(delegation or {})), _routing(**(routing or {})))


def _c17(**over: Any) -> tuple[ModelCheckClause, ...]:
    return grade_c17(_surface(**over))


def _failed(clauses: tuple[ModelCheckClause, ...]) -> list[str]:
    return [c.name for c in clauses if not c.ok]


def _verdict(clauses: tuple[ModelCheckClause, ...]) -> EnumCheckVerdict:
    return ModelDelegationCheckReading(clauses=clauses).verdict()


# ----- the fixture names its source ---------------------------------------------------------------------------------


def test_the_fixture_covers_the_ten_checks() -> None:
    assert tuple(GOLDEN["checks"]) == TEN_CHECKS


def test_every_answer_names_its_source() -> None:
    assert GOLDEN["machine"]["source"]["kind"] in SOURCES
    answers = GOLDEN["gateway"]["answers"]
    assert answers
    for name, answer in answers.items():
        assert answer["source"]["kind"] in SOURCES, name
        if answer["source"]["kind"] == SOURCES[0]:
            assert answer["source"]["run"] == "C11 run 45351153694", name
            assert answer["source"]["captured_at"], name


def test_the_constructed_gateway_answers_validate_against_the_declared_models() -> None:
    answers = GOLDEN["gateway"]["answers"]
    ModelWorkflowAck.model_validate(answers["submit_healthy"]["body"])
    ModelWorkflowAck.model_validate(answers["submit_dying"]["body"])
    for key in ("status_healthy", "status_dying"):
        ModelWorkflowStatus.model_validate(answers[key]["body"])
    for key in ("receipt_healthy", "receipt_dying"):
        ModelWorkflowReceipt.model_validate(answers[key]["body"])
    assert answers["status_healthy"]["body"]["status"] == "completed"
    assert answers["receipt_healthy"]["body"]["terminal_model_used"] == SERVED
    assert answers["status_dying"]["body"]["status"] == "failed"


def test_the_recorded_c11_answers_carry_the_statuses_and_codes_the_run_read() -> None:
    answers = GOLDEN["gateway"]["answers"]
    assert answers["health"]["status"] == 200
    assert (answers["wrong_key"]["status"], answers["wrong_key"]["body"]["code"]) == (
        401,
        "auth.credential.invalid",
    )
    assert (answers["absent_key"]["status"], answers["absent_key"]["body"]["code"]) == (
        401,
        "auth.credential.absent",
    )
    assert (
        answers["wrong_tenant"]["status"],
        answers["wrong_tenant"]["body"]["code"],
    ) == (404, "resource.not_found")
    assert answers["malformed"]["status"] == 422


# ----- a good answer grades PASS ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("check", "clauses"),
    [("C13", _c13), ("C14", _c14), ("C17", _c17)],
)
def test_the_good_answer_grades_pass(check: str, clauses: Any) -> None:
    graded = clauses()
    assert _failed(graded) == [], check
    assert _verdict(graded) is EnumCheckVerdict.PASS


# ----- the same answer with one clause removed grades FAIL and names that clause ------------------------------------

_BAD_BROKEN = {"entry_point": "c17-broken", "rc": 1}
REMOVALS: list[tuple[str, str, str, Any]] = [
    ("C13", "no_checkout", "no_checkout false", lambda: _c13(no_checkout=False)),
    (
        "C13",
        "install_entry",
        "entry not the install's own",
        lambda: _c13(entry_is_install_own=False, entry_path="/opt/runner/bin/onex"),
    ),
    ("C13", "three_files", "files disagree", lambda: _c13(three_files_agree=False)),
    ("C13", "three_files", "delegate rc 1", lambda: _c13(rc=1)),
    (
        "C13",
        "local_model",
        "receipt tier not local",
        lambda: _c13(receipt_tier="openrouter"),
    ),
    (
        "C13",
        "local_model",
        "receipt names another model",
        lambda: _c13(receipt_model="other-model"),
    ),
    (
        "C13",
        "local_model",
        "ladder holds a non-local rung",
        lambda: _c13(attempt_tiers=("local", "openrouter")),
    ),
    (
        "C13",
        "zero_provider",
        "egress instrument recorded nothing",
        lambda: _c13(egress_recorded=False),
    ),
    (
        "C13",
        "zero_provider",
        "no connect to the responder",
        lambda: _c13(model_connects=0),
    ),
    (
        "C13",
        "zero_provider",
        "token counter did not move",
        lambda: _c13(token_counter_moved=False),
    ),
    (
        "C13",
        "zero_provider",
        "positive control not classified external",
        lambda: _c13(control_external_classified=False),
    ),
    (
        "C13",
        "zero_provider",
        "external connect recorded",
        lambda: _c13(egress_external=("api.provider.example:443",)),
    ),
    ("C14", "no_checkout", "no_checkout false", lambda: _c14({"no_checkout": False})),
    (
        "C14",
        "row1_local_answer_ends_chain",
        "first rung not local",
        lambda: _c14({"attempt_tiers": ("openrouter",)}),
    ),
    (
        "C14",
        "row1_local_answer_ends_chain",
        "receipt tier not local",
        lambda: _c14({"receipt_tier": "openrouter"}),
    ),
    (
        "C14",
        "row1_local_answer_ends_chain",
        "accepted not last",
        lambda: _c14({"accepted_last": False}),
    ),
    (
        "C14",
        "row1_local_answer_ends_chain",
        "escalated",
        lambda: _c14({"escalated": True}),
    ),
    (
        "C14",
        "row1_local_answer_ends_chain",
        "cost above zero",
        lambda: _c14({"cost_usd": 0.01}),
    ),
    (
        "C14",
        "row2_no_unbindable_tier",
        "a task class unbindable",
        lambda: _c14(routing={"unbindable": ("document",)}),
    ),
    (
        "C14",
        "row2_no_unbindable_tier",
        "undeclared control not classified",
        lambda: _c14(routing={"undeclared_control_classified_deleted": False}),
    ),
    (
        "C14",
        "row2_no_unbindable_tier",
        "closed-port control not classified",
        lambda: _c14(routing={"closed_port_control_classified_unreachable": False}),
    ),
    (
        "C14",
        "row4_reviewer_unmetered",
        "judge endpoint not private",
        lambda: _c14(routing={"judge_endpoint_private": False}),
    ),
    (
        "C14",
        "row4_reviewer_unmetered",
        "judge needs a credential",
        lambda: _c14(routing={"judge_needs_credential": True}),
    ),
    (
        "C14",
        "row4_reviewer_unmetered",
        "judge serves no model",
        lambda: _c14(routing={"judge_serves_model": False}),
    ),
    (
        "C14",
        "row4_reviewer_unmetered",
        "external egress",
        lambda: _c14({"egress_external": ("api.provider.example:443",)}),
    ),
    (
        "C14",
        "row4_reviewer_unmetered",
        "egress instrument absent",
        lambda: _c14({"egress_recorded": False}),
    ),
    (
        "C17",
        "guides_lead_with_local_path",
        "guide leads with a cloud command",
        lambda: _c17(guides_lead_with_local=False),
    ),
    (
        "C17",
        "clean_install_named_interpreter",
        "install rc 1",
        lambda: _c17(install_rc=1),
    ),
    (
        "C17",
        "clean_install_named_interpreter",
        "interpreter mismatch",
        lambda: _c17(install_interpreter_matches=False),
    ),
    (
        "C17",
        "clean_install_named_interpreter",
        "help does not answer",
        lambda: _c17(help_answers=False),
    ),
    (
        "C17",
        "parameter_audit",
        "internal parameter found",
        lambda: _c17(parameter_findings=("cli|onex x --flag|p1|text",)),
    ),
    (
        "C17",
        "parameter_audit",
        "controls not caught",
        lambda: _c17(parameter_controls_caught=False),
    ),
    (
        "C17",
        "broken_command_named_error",
        "no broken-command run recorded",
        lambda: _c17(broken_cases=()),
    ),
    (
        "C17",
        "broken_command_named_error",
        "broken entry exits 0",
        lambda: _c17(
            broken_cases=(
                {**_BAD_BROKEN, "rc": 0, "output": "Error: c17-broken failed"},
            )
        ),
    ),
    (
        "C17",
        "broken_command_named_error",
        "error does not name the entry point",
        lambda: _c17(
            broken_cases=({**_BAD_BROKEN, "output": "Error: something failed"},)
        ),
    ),
    (
        "C17",
        "broken_command_named_error",
        "answered No such command without the switch",
        lambda: _c17(
            broken_cases=({**_BAD_BROKEN, "output": "No such command 'c17-broken'"},),
            broken_ran_with_switch=False,
        ),
    ),
    (
        "C17",
        "broken_command_named_error",
        "onex not restored after removal",
        lambda: _c17(broken_after_rc=1),
    ),
]


@pytest.mark.parametrize(
    ("check", "clause", "removed", "grade"),
    REMOVALS,
    ids=[f"{c}-{k}-{r}" for c, k, r, _ in REMOVALS],
)
def test_a_clause_removed_alone_fails_and_names_that_clause(
    check: str, clause: str, removed: str, grade: Any
) -> None:
    graded = grade()
    assert _verdict(graded) is EnumCheckVerdict.FAIL, (check, removed)
    assert _failed(graded) == [clause], (check, removed)


def test_every_clause_of_the_three_customer_machine_graders_is_removed_by_some_case() -> (
    None
):
    covered = {(c, k) for c, k, _, _ in REMOVALS}
    for check, clauses in (("C13", _c13()), ("C14", _c14()), ("C17", _c17())):
        for clause in clauses:
            assert (check, clause.name) in covered, (check, clause.name)


# ----- AC3: an empty answer, a timeout and a missing credential are never PASS ---------------------------------------


@pytest.mark.parametrize(
    "clauses",
    [
        lambda: grade_c13(
            ModelDelegateObservation(rc=0),
            responder_base=RESPONDER,
            served_model=SERVED,
        ),
        lambda: grade_c13(
            ModelDelegateObservation(rc=124),
            responder_base=RESPONDER,
            served_model=SERVED,
        ),
        lambda: grade_c14(ModelDelegateObservation(rc=0), ModelRoutingCollection()),
        lambda: grade_c14(ModelDelegateObservation(rc=124), ModelRoutingCollection()),
        lambda: grade_c17(ModelSurfaceObservation()),
        lambda: grade_c17(ModelSurfaceObservation(install_rc=None)),
    ],
    ids=[
        "c13-empty",
        "c13-timeout",
        "c14-empty",
        "c14-timeout",
        "c17-empty",
        "c17-no-install",
    ],
)
def test_an_empty_or_timed_out_observation_grades_fail(clauses: Any) -> None:
    graded = clauses()
    assert _verdict(graded) is EnumCheckVerdict.FAIL
    assert len(_failed(graded)) >= 3


_LOCAL = ModelDelegationCheckResponder(tier="local", base_url=RESPONDER)
_RESPONDER_READING = {
    "base_url": RESPONDER,
    "answered": True,
    "served_model": SERVED,
    "max_model_len": 65536,
}


class _Timeout(InMemoryCustomerMachine):
    def run_delegation(self, check_id: CheckId, responder: Any) -> Any:
        raise TimeoutError("the delegate did not finish in its budget")

    def collect_surface(self) -> Any:
        raise TimeoutError("the clean install did not finish in its budget")


def _handler(machine: Any) -> HandlerDelegationCheckProbe:
    from omnimarket.handlers.routing.delegation_check_responder_chain import (
        ModelResponderReading,
    )

    clock = FakeClock()
    return HandlerDelegationCheckProbe(
        responders=(_LOCAL,),
        reader=InMemoryResponderReader(
            {RESPONDER: ModelResponderReading(**_RESPONDER_READING)}
        ),
        machine=machine,
        publisher=InMemoryCheckPublisher(),
        topic=contract_topics()[0],
        clock=lambda: datetime(2026, 11, 24, 15, 0, tzinfo=UTC),
        gateway=FakeGateway(),
        gateway_settings=GatewayRunSettings(run_budget_s=5.0, poll_interval_s=0.0),
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


@pytest.mark.parametrize("check", ["C13", "C14", "C17"])
def test_the_good_answer_through_the_handler_reads_pass(check: str) -> None:
    machine = InMemoryCustomerMachine(
        delegation=_delegation(), routing=_routing(), surface=_surface()
    )
    result = _handler(machine).handle(
        ModelDelegationCheckRequest(correlation_id="g-1", check_id=cast(CheckId, check))
    )
    assert result.verdict is EnumCheckVerdict.PASS, result.error
    assert result.conclusion == "success"


@pytest.mark.parametrize("check", ["C13", "C14", "C17"])
def test_an_empty_answer_through_the_handler_never_reads_pass(check: str) -> None:
    machine = InMemoryCustomerMachine(
        delegation=ModelDelegateObservation(rc=0),
        routing=ModelRoutingCollection(),
        surface=ModelSurfaceObservation(),
    )
    result = _handler(machine).handle(
        ModelDelegationCheckRequest(correlation_id="g-2", check_id=cast(CheckId, check))
    )
    assert result.verdict is EnumCheckVerdict.FAIL
    assert result.conclusion == "failure"


@pytest.mark.parametrize("check", ["C13", "C14", "C17"])
def test_a_timeout_through_the_handler_reads_error_never_pass(check: str) -> None:
    result = _handler(_Timeout()).handle(
        ModelDelegationCheckRequest(correlation_id="g-3", check_id=cast(CheckId, check))
    )
    assert result.verdict is EnumCheckVerdict.ERROR
    assert result.conclusion == "failure"
    assert result.error is not None
    assert "TimeoutError" in result.error


@pytest.mark.parametrize("check", ["C13", "C14", "C17"])
def test_a_missing_credential_or_collector_reads_precondition_absent(
    check: str,
) -> None:
    class _NoCredential(AbsentCustomerMachine):
        def __init__(self) -> None:
            super().__init__("the tenant credential is not in the store")

    result = _handler(_NoCredential()).handle(
        ModelDelegationCheckRequest(correlation_id="g-4", check_id=cast(CheckId, check))
    )
    assert result.verdict is EnumCheckVerdict.PRECONDITION_ABSENT
    assert result.conclusion == "failure"
    assert CollectorAbsentError.__name__


# ----- the credential case: the real caller-identity resolver -------------------------------------------------------

_ONXK = "onxk_" + "ab12cd34" * 4
_JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJkZXYtcHJvb2YtY2xpLXJ1bi1hIn0.c2lnbmF0dXJl"


class _KeyTable:
    """The tenant_api_keys table as the real resolver queries it: one active row per registered sha256 digest."""

    def __init__(self, keys: dict[str, tuple[UUID, str]]) -> None:
        self._rows = {
            hashlib.sha256(k.encode()).hexdigest(): v for k, v in keys.items()
        }

    async def fetchrow(self, _sql: str, key_hash: str) -> tuple[UUID, str, UUID] | None:
        row = self._rows.get(key_hash)
        return None if row is None else (row[0], row[1], UUID(int=7))

    async def execute(self, *_args: Any) -> None:
        return None


def _contracts_status(api_key: str | None) -> tuple[int, Any]:
    gateway = build_gateway()
    identity = CompositeCallerIdentity(
        bearer=None,
        api_key=PostgresApiKeyTenantIdentity(
            pool=cast(Any, _KeyTable({_ONXK: (TENANT_A, "tenant-a")})),
            header="x-api-key",
            prefix="onxk_",
        ),
        api_key_header="x-api-key",
    )
    app = create_app(dependencies=replace(gateway.deps, identity=identity))

    async def _get() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://runtime"
        ) as client:
            headers = {} if api_key is None else {"x-api-key": api_key}
            return await client.get("/v1/workflows/contracts", headers=headers)

    response = asyncio.run(_get())
    return response.status_code, response.json()


def test_a_registered_onxk_key_in_x_api_key_is_accepted() -> None:
    status, body = _contracts_status(_ONXK)
    assert status == 200
    assert {c["workflow_type"] for c in body["contracts"]} >= {"delegate-skill"}


def test_a_jwt_in_x_api_key_is_refused_with_the_code_a_wrong_key_gets() -> None:
    status, body = _contracts_status(_JWT)
    wrong_key = GOLDEN["gateway"]["answers"]["wrong_key"]
    assert (status, body["code"]) == (wrong_key["status"], wrong_key["body"]["code"])


def test_no_key_is_refused_with_the_absent_code() -> None:
    status, body = _contracts_status(None)
    absent = GOLDEN["gateway"]["answers"]["absent_key"]
    assert (status, body["code"]) == (absent["status"], absent["body"]["code"])
