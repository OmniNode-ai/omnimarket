# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The cloud-dependent delegation checks are graded through the dev gateway, and only through it (OMN-26642).

C11, C12, C15, C16, C28, C29 and C30 used to be graded by cloud workflows. The node grades them now by talking to the system
the way a customer does: the gateway address from the settings store, the test tenant's credential from the dev tenant node.
Every test here uses an in-memory gateway; the live readback is in the PR body.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import SecretStr

from omnimarket.models.overlay_documents.model_delegation_check_gateway_overlay import (
    ModelDelegationCheckGatewayOverlay,
    ModelDevTenantTokenSource,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_delegation_check import (
    AbsentCustomerMachine,
    InMemoryCheckPublisher,
    InMemoryResponderReader,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.adapter_gateway import (
    DevTenantCredential,
    HttpGatewayClient,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.gateway_checks import (
    CLOUD_DEPENDENT_CHECKS,
    GATEWAY_CHECKS,
    GatewayRunSettings,
    resolve_check_route,
    run_gateway_check,
)
from omnimarket.nodes.node_delegation_check_probe_effect.handlers.handler_delegation_check_probe import (
    HandlerDelegationCheckProbe,
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

GATEWAY = "http://gateway.test.example:4100"
TENANT = "test-tenant-a"
HANDLERS = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_delegation_check_probe_effect"
)
SETTINGS = GatewayRunSettings(run_budget_s=5.0, poll_interval_s=0.0)
UUID_RE = re.compile(r"^[0-9a-f-]{36}$")


class FakeGateway:
    """An in-memory gateway answering the routes a customer reaches; ``override`` replaces one answer."""

    def __init__(self, url: str = GATEWAY, tenant: str = TENANT) -> None:
        self.gateway_url = url
        self.tenant_slug = tenant
        self.calls: list[tuple[str, str, str]] = []
        self.override: Callable[
            [str, str, str, Any], ModelGatewayObservation | None
        ] = lambda _method, _path, _auth, _body: None
        self._runs: dict[str, dict[str, Any]] = {}

    def call(
        self, method: str, path: str, *, auth: str, body: dict[str, Any] | None = None
    ) -> ModelGatewayObservation:
        self.calls.append((method, path, auth))
        replaced = self.override(method, path, auth, body)
        if replaced is not None:
            return replaced
        return self._answer(method, path, auth, body)

    def _answer(
        self, method: str, path: str, auth: str, body: dict[str, Any] | None
    ) -> ModelGatewayObservation:
        if path == "/health":
            return ModelGatewayObservation(status=200, body={"status": "ok"})
        if auth == "none":
            return ModelGatewayObservation(
                status=401, body={"code": "auth.credential.absent"}
            )
        if auth == "invalid":
            return ModelGatewayObservation(
                status=401, body={"code": "auth.credential.invalid"}
            )
        if path == "/v1/workflows/contracts":
            return ModelGatewayObservation(
                status=200,
                body={
                    "contracts": [
                        {"workflow_type": "delegate-skill", "version": "1"},
                        {"workflow_type": "ledger-append", "version": "1"},
                    ]
                },
            )
        if method == "POST" and path == "/v1/workflows":
            assert body is not None
            payload = body["payload"]
            source = payload.get("source")
            if source not in ("claude-code", "codex", "external-client"):
                return ModelGatewayObservation(
                    status=422,
                    body={
                        "detail": [
                            {
                                "type": "literal_error",
                                "loc": ["body", "payload", "source"],
                            }
                        ]
                    },
                )
            workflow_id = f"00000000-0000-4000-8000-{len(self._runs):012d}"
            dying = payload["task_type"] == "omn19181_not_a_task_class"
            self._runs[workflow_id] = {"dying": dying}
            return ModelGatewayObservation(
                status=202, body={"workflow_id": workflow_id}
            )
        match = re.match(r"^/v1/workflows/([^/]+)/(status|receipt)", path)
        if match and match.group(1) in self._runs:
            dying = self._runs[match.group(1)]["dying"]
            if match.group(2) == "status":
                return ModelGatewayObservation(
                    status=200,
                    body={
                        "status": "failed" if dying else "completed",
                        **(
                            {
                                "terminal_failure_class": "TaskClassError",
                                "terminal_failure_code": "ONEX_TASK_CLASS_UNKNOWN",
                            }
                            if dying
                            else {"route": "lab-local", "provider": "local"}
                        ),
                    },
                )
            if dying:
                return ModelGatewayObservation(
                    status=200,
                    body={
                        "status": "failed",
                        "terminal_failure_class": "TaskClassError",
                        "terminal_failure_code": "ONEX_TASK_CLASS_UNKNOWN",
                    },
                )
            return ModelGatewayObservation(
                status=200,
                body={
                    "status": "completed",
                    "route": "lab-local",
                    "provider": "local",
                    "terminal_model_used": "served-model",
                    "rule_evaluations": [
                        {"rule": "non_empty", "passed": True, "enforcement": "blocking"}
                    ],
                },
            )
        if path.startswith("/v1/workflows/") and path.endswith("/receipt"):
            # An unknown run asked with no runner identity: the request is refused as malformed before it is looked up.
            return ModelGatewayObservation(
                status=422,
                body={
                    "detail": [{"type": "missing", "loc": ["query", "runner_identity"]}]
                },
            )
        return ModelGatewayObservation(status=404, body={"code": "resource.not_found"})


class _Clock:
    """A clock that moves one second per sleep, so a budget of five seconds is five polls and no real time."""

    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, _s: float) -> None:
        self.now += 1.0

    def monotonic(self) -> float:
        return self.now


def _run(check: str, gateway: FakeGateway):
    clock = _Clock()
    return run_gateway_check(
        check,  # type: ignore[arg-type]
        gateway,
        SETTINGS,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
    )


# AC1: each cloud-dependent check resolves to the gateway address read from the settings store, never to a workflow, host or board.
@pytest.mark.unit
@pytest.mark.parametrize("check", CLOUD_DEPENDENT_CHECKS)
def test_each_cloud_dependent_check_resolves_to_the_settings_store_gateway(
    check: str,
) -> None:
    overlay = ModelDelegationCheckGatewayOverlay(
        schema_version=1, gateway_url=GATEWAY, tenant_slug=TENANT
    )
    route = resolve_check_route(check, overlay.gateway_url)  # type: ignore[arg-type]
    assert route.endpoint == overlay.gateway_url
    assert route.runner == "omnimarket-node"
    assert route.board == "omnimarket-beta-board"
    blob = repr(route).lower()
    for cloud in ("github", "workflow_dispatch", "actions", "self-hosted"):
        assert cloud not in blob


@pytest.mark.unit
def test_the_ten_checks_are_the_seven_gateway_checks_and_the_three_customer_local_ones() -> (
    None
):
    assert set(GATEWAY_CHECKS) == {"C11", "C12", "C15", "C16", "C28", "C29", "C30"}
    assert set(CLOUD_DEPENDENT_CHECKS) == set(GATEWAY_CHECKS)
    for local in ("C13", "C14", "C17"):
        route = resolve_check_route(local, GATEWAY)  # type: ignore[arg-type]
        assert route.runner == "omnimarket-node"
        assert route.endpoint != GATEWAY


@pytest.mark.unit
@pytest.mark.parametrize("check", GATEWAY_CHECKS)
def test_every_request_of_a_check_goes_to_the_one_gateway_client(check: str) -> None:
    gateway = FakeGateway(url="http://other.test.example:4999")
    reading = _run(check, gateway)
    assert gateway.calls, "the check made no gateway call"
    assert reading.endpoint == "http://other.test.example:4999"


@pytest.mark.unit
@pytest.mark.parametrize("check", GATEWAY_CHECKS)
def test_a_healthy_gateway_grades_every_check_pass(check: str) -> None:
    reading = _run(check, FakeGateway())
    failed = [c.name for c in reading.clauses if not c.ok]
    assert reading.verdict() is EnumCheckVerdict.PASS, failed
    assert reading.clauses


# Falsifiers: each check goes red when the gateway breaks the thing it grades.
def _break(check: str, override: Callable[..., Any], expect_clause: str) -> None:
    gateway = FakeGateway()
    gateway.override = override
    reading = _run(check, gateway)
    assert reading.verdict() is EnumCheckVerdict.FAIL
    assert expect_clause in [c.name for c in reading.clauses if not c.ok]


@pytest.mark.unit
def test_c11_fails_when_a_wrong_key_is_accepted() -> None:
    _break(
        "C11",
        lambda _m, _p, a, _b: (
            ModelGatewayObservation(status=200, body={}) if a == "invalid" else None
        ),
        "wrong_key",
    )


@pytest.mark.unit
def test_c11_fails_when_the_gateway_answers_nothing_but_refusals() -> None:
    # The positive control: a dead endpoint refusing everything must not grade PASS.
    _break(
        "C11",
        lambda _m, p, _a, _b: (
            ModelGatewayObservation(
                status=401, body={"code": "auth.credential.invalid"}
            )
            if p == "/health"
            else None
        ),
        "positive_control",
    )


@pytest.mark.unit
def test_c12_fails_when_a_contract_names_a_house_credential() -> None:
    _break(
        "C12",
        lambda _m, p, a, _b: (
            ModelGatewayObservation(
                status=200,
                body={
                    "contracts": [
                        {
                            "workflow_type": "delegate-skill",
                            "secret_ref": "llm.openrouter.api_key",
                        }
                    ]
                },
            )
            if p == "/v1/workflows/contracts" and a == "tenant"
            else None
        ),
        "no_house_entry",
    )


@pytest.mark.unit
def test_c15_fails_when_the_receipt_names_no_route() -> None:
    def no_route(m: str, p: str, a: str, b: Any) -> ModelGatewayObservation | None:
        if p.endswith("/receipt") or "/receipt?" in p:
            return ModelGatewayObservation(
                status=200,
                body={
                    "status": "completed",
                    "provider": "local",
                    "terminal_model_used": "m",
                },
            )
        return None

    _break("C15", no_route, "receipt_names_route_provider_model")


@pytest.mark.unit
def test_c16_fails_when_a_dead_run_reads_completed() -> None:
    def completed_when_dying(
        m: str, p: str, a: str, b: Any
    ) -> ModelGatewayObservation | None:
        if "/status" in p:
            return ModelGatewayObservation(status=200, body={"status": "completed"})
        return None

    gateway = FakeGateway()
    gateway.override = completed_when_dying
    reading = _run("C16", gateway)
    assert reading.verdict() is EnumCheckVerdict.FAIL


@pytest.mark.unit
def test_c28_fails_when_an_invalid_payload_is_discarded_and_never_terminal() -> None:
    gateway = FakeGateway()

    def stuck(m: str, p: str, a: str, b: Any) -> ModelGatewayObservation | None:
        if (
            "/status" in p
            and gateway._runs
            and list(gateway._runs.values())[-1]["dying"]
        ):
            return ModelGatewayObservation(status=200, body={"status": "published"})
        return None

    gateway.override = stuck
    reading = _run("C28", gateway)
    assert reading.verdict() is EnumCheckVerdict.FAIL
    assert "invalid_payload_observed" in [c.name for c in reading.clauses if not c.ok]


@pytest.mark.unit
def test_c29_fails_when_an_invalid_key_is_accepted_for_delegation() -> None:
    _break(
        "C29",
        lambda m, _p, a, _b: (
            ModelGatewayObservation(status=202, body={"workflow_id": "x"})
            if m == "POST" and a == "invalid"
            else None
        ),
        "house_key_never_stands_in",
    )


@pytest.mark.unit
def test_c30_fails_when_an_unknown_source_is_accepted() -> None:
    gateway = FakeGateway()

    def accept_all(m: str, p: str, a: str, b: Any) -> ModelGatewayObservation | None:
        if m == "POST" and b and b["payload"].get("source") == "not-a-source":
            return ModelGatewayObservation(status=202, body={"workflow_id": "x"})
        return None

    gateway.override = accept_all
    reading = _run("C30", gateway)
    assert reading.verdict() is EnumCheckVerdict.FAIL


@pytest.mark.unit
def test_a_gateway_that_cannot_be_reached_is_not_measured_never_green() -> None:
    gateway = FakeGateway()
    gateway.override = lambda _m, _p, _a, _b: ModelGatewayObservation(
        status=None, error="URLError"
    )
    reading = _run("C15", gateway)
    assert reading.verdict() is EnumCheckVerdict.NOT_MEASURED


_CLOCK = _Clock()


# The handler path: the run record names the gateway address and the tenant.
def _handler(gateway: FakeGateway | None) -> HandlerDelegationCheckProbe:
    return HandlerDelegationCheckProbe(
        responders=(),
        reader=InMemoryResponderReader({}),
        machine=AbsentCustomerMachine("none"),
        publisher=InMemoryCheckPublisher(),
        topic="onex.evt.omnimarket.delegation-check-probe-completed.v1",
        clock=lambda: datetime(2026, 11, 24, 13, 0, tzinfo=UTC),
        gateway=gateway,
        gateway_settings=SETTINGS,
        sleep=_CLOCK.sleep,
        monotonic=_CLOCK.monotonic,
    )


@pytest.mark.unit
@pytest.mark.parametrize("check", GATEWAY_CHECKS)
def test_the_run_record_names_the_gateway_address_and_the_tenant(check: str) -> None:
    result = _handler(FakeGateway()).handle(
        ModelDelegationCheckRequest(correlation_id="c-1", check_id=check)  # type: ignore[arg-type]
    )
    assert result.verdict is EnumCheckVerdict.PASS, result.error
    assert result.gateway_url == GATEWAY
    assert result.tenant_slug == TENANT
    assert result.runner == "omnimarket-node"


@pytest.mark.unit
@pytest.mark.parametrize("check", GATEWAY_CHECKS)
def test_with_no_gateway_row_the_check_is_precondition_absent_never_green(
    check: str,
) -> None:
    result = _handler(None).handle(
        ModelDelegationCheckRequest(correlation_id="c-2", check_id=check)  # type: ignore[arg-type]
    )
    assert result.verdict is EnumCheckVerdict.PRECONDITION_ABSENT
    assert result.conclusion == "failure"
    assert result.gateway_url is None


# AC2: no literal host, address or port in the check node's handlers; the overlay refuses a loopback or host-local gateway.
_HOST_LITERAL = re.compile(
    r"""(https?://[A-Za-z0-9._-]+|\b\d{1,3}(?:\.\d{1,3}){3}\b|\blocalhost\b|\bhost\.docker\.internal\b|[A-Za-z0-9.-]+:\d{2,5}\b)"""
)


@pytest.mark.unit
def test_no_handler_of_the_check_node_names_a_host_address_or_port() -> None:
    offenders: list[str] = []
    files = [
        *sorted((HANDLERS / "handlers").glob("*.py")),
        HANDLERS / "runtime_delegation_check_probe_tick.py",
    ]
    for path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if _HOST_LITERAL.search(code):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, offenders


@pytest.mark.unit
def test_the_host_literal_scan_bites_on_a_planted_literal() -> None:
    assert _HOST_LITERAL.search('base = "http://gateway.example:8085"')
    assert _HOST_LITERAL.search("x = 'gateway.example:8085'")
    assert not _HOST_LITERAL.search("path = '/v1/workflows'")


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    [
        "http://localhost:8085",
        "http://127.0.0.1:8085",
        "http://gateway.docker.internal:80",
    ],
)
def test_the_overlay_refuses_a_host_local_gateway(bad: str) -> None:
    with pytest.raises(ValueError, match=r".+"):
        ModelDelegationCheckGatewayOverlay(
            schema_version=1, gateway_url=bad, tenant_slug=TENANT
        )


@pytest.mark.unit
def test_the_overlay_requires_an_explicit_port_and_no_default_address() -> None:
    with pytest.raises(ValueError, match=r".+"):
        ModelDelegationCheckGatewayOverlay(
            schema_version=1,
            gateway_url="http://gateway.test.example",
            tenant_slug=TENANT,
        )
    with pytest.raises(ValueError, match=r".+"):
        ModelDelegationCheckGatewayOverlay(schema_version=1, tenant_slug=TENANT)  # type: ignore[call-arg]


# The tenant credential: the dev tenant node's overlay names the secret, the secret store holds the token.
class _Reader:
    def __init__(self, value: str | None) -> None:
        self.value = value
        self.read_names: list[tuple[str, str]] = []

    async def read(self, folder: str, name: str) -> SecretStr | None:
        self.read_names.append((folder, name))
        return SecretStr(self.value) if self.value is not None else None


def _dev_tenant_source() -> ModelDevTenantTokenSource:
    return ModelDevTenantTokenSource.model_validate(
        {
            "store": {"provider": "infisical"},
            "token_folder": "/shared/env",
            "tenants": [
                {
                    "tenant_slug": TENANT,
                    "tenant_id": "ignored",
                    "secret_name": "DEV_TENANT_SECRET_NAME",
                }
            ],
        }
    )


@pytest.mark.unit
def test_the_tenant_token_is_read_by_the_secret_name_the_dev_tenant_node_declares() -> (
    None
):
    reader = _Reader("tok-value")
    credential = DevTenantCredential(_dev_tenant_source(), reader)
    assert credential.token(TENANT) == "tok-value"
    assert reader.read_names == [("/shared/env", "DEV_TENANT_SECRET_NAME")]


@pytest.mark.unit
def test_a_tenant_the_dev_tenant_node_does_not_declare_is_precondition_absent() -> None:
    with pytest.raises(CollectorAbsentError, match="tenant"):
        DevTenantCredential(_dev_tenant_source(), _Reader("x")).token("another-tenant")


@pytest.mark.unit
def test_a_token_missing_from_the_store_is_precondition_absent() -> None:
    with pytest.raises(CollectorAbsentError, match="not in the dev secret store"):
        DevTenantCredential(_dev_tenant_source(), _Reader(None)).token(TENANT)


@pytest.mark.unit
@pytest.mark.parametrize("check", GATEWAY_CHECKS)
def test_with_no_tenant_token_the_run_record_still_names_the_gateway_and_is_never_green(
    check: str,
) -> None:
    class _NoToken(FakeGateway):
        def call(self, method, path, *, auth, body=None):  # type: ignore[no-untyped-def]
            if auth == "tenant":
                raise CollectorAbsentError("tenant_credential", "no token")
            return super().call(method, path, auth=auth, body=body)

    result = _handler(_NoToken()).handle(
        ModelDelegationCheckRequest(correlation_id="c-3", check_id=check)  # type: ignore[arg-type]
    )
    assert result.verdict is EnumCheckVerdict.PRECONDITION_ABSENT
    assert result.conclusion == "failure"
    assert result.gateway_url == GATEWAY
    assert result.tenant_slug == TENANT


@pytest.mark.unit
def test_the_http_client_sends_the_tenant_token_only_in_the_api_key_header() -> None:
    class _Echo(BaseHTTPRequestHandler):
        seen: ClassVar[list[dict[str, str]]] = []

        def do_GET(self) -> None:
            _Echo.seen.append({k.lower(): v for k, v in self.headers.items()})
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *_a: object) -> None:
            return None

    server = HTTPServer(("127.0.0.1", 0), _Echo)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        row = ModelDelegationCheckGatewayOverlay.model_construct(
            schema_version=1,
            gateway_url=f"http://127.0.0.1:{server.server_address[1]}",
            tenant_slug=TENANT,
        )
        client = HttpGatewayClient(
            row, DevTenantCredential(_dev_tenant_source(), _Reader("tok-value")), 5.0
        )
        answered = client.call("GET", "/v1/workflows/contracts", auth="tenant")
        anonymous = client.call("GET", "/health", auth="none")
    finally:
        server.shutdown()
    assert answered.status == 200
    assert anonymous.status == 200
    assert _Echo.seen[0]["x-api-key"] == "tok-value"
    assert "authorization" not in _Echo.seen[0]
    assert "x-api-key" not in _Echo.seen[1]


@pytest.mark.unit
def test_the_http_client_reports_an_unreachable_gateway_as_no_answer() -> None:
    row = ModelDelegationCheckGatewayOverlay.model_construct(
        schema_version=1, gateway_url="http://127.0.0.1:9", tenant_slug=TENANT
    )
    client = HttpGatewayClient(
        row, DevTenantCredential(_dev_tenant_source(), _Reader("t")), 0.5
    )
    answered = client.call("GET", "/health", auth="none")
    assert answered.status is None
    assert answered.error
