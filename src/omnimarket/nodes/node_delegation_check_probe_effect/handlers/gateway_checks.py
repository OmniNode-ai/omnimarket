# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Grade the cloud-dependent delegation checks through the dev gateway, the way a customer reaches the system (OMN-26642).

C11, C12, C15, C16, C28, C29 and C30 used to be graded by cloud workflows (a scheduled job on a cloud runner, a repository
secret, a repository variable, a board reading the job's conclusion). Each is graded here by requests to one gateway client,
whose address and test tenant come from the settings store: nothing in this module names a host, an address or a port, and no
check reaches the system any other way. A request that gets no answer is NOT_MEASURED (the lab, not the product, did not
answer); a missing tenant credential is PRECONDITION-ABSENT; a clause that does not hold is FAIL.

Each clause below is the gateway-observable part of the cloud workflow's own criterion. What a workflow measured from inside
the deployed runtime (the in-process injections of C12, the broker scan of C16, the consumer-flow exposure walk of C28, the
egress recorder of C29 and the plugin install of C30) is not observable through the gateway; those clauses stay with the cloud
variants, which the node's contract lists as checks for the next milestone.
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any, Final, cast, get_args

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    CheckId,
    ModelCheckClause,
    ModelDelegationCheckReading,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_gateway_check import (
    ModelCheckRoute,
    ModelGatewayObservation,
)
from omnimarket.nodes.node_delegation_check_probe_effect.protocols.protocol_delegation_check import (
    ProtocolGatewayClient,
)

#: The seven checks a cloud workflow graded; the node grades them through the gateway alone.
GATEWAY_ONLY_CHECKS: Final[tuple[CheckId, ...]] = (
    "C11",
    "C12",
    "C15",
    "C16",
    "C28",
    "C29",
    "C30",
)
CLOUD_DEPENDENT_CHECKS: Final[tuple[CheckId, ...]] = GATEWAY_ONLY_CHECKS
#: The three checks also graded on a customer machine through the responder chain the settings store declares. Each has a
#: gateway leg as well: the customer's own view of the same behaviour, from the gateway (operator message 2026-11-24T13:54:22Z).
CUSTOMER_LOCAL_CHECKS: Final[tuple[CheckId, ...]] = ("C13", "C14", "C17")
#: All ten delegation checks: every one of them is graded through the dev gateway.
GATEWAY_CHECKS: Final[tuple[CheckId, ...]] = tuple(
    sorted(
        (*GATEWAY_ONLY_CHECKS, *CUSTOMER_LOCAL_CHECKS),
        key=lambda c: int(c[1:]),
    )
)

RUNNER: Final = "omnimarket-node"
BOARD: Final = "omnimarket-beta-board"

WORKFLOW_TYPE: Final = "delegate-skill"
TERMINAL: Final = frozenset({"completed", "failed"})
SOURCES: Final = ("claude-code", "codex", "external-client")
DYING_TASK_TYPE: Final = "omn19181_not_a_task_class"
UNKNOWN_SOURCE: Final = "not-a-source"
HOUSE_REF = re.compile(r"^llm\.[a-z0-9_]+\.[a-z0-9_]+$")
FAILURE_CLASS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(Error|Exception)$")
FAILURE_CODE = re.compile(r"^ONEX_[A-Z0-9_]+$")
ABSENT_ID: Final = "00000000-0000-4000-8000-0000000000{n:02d}"
RUNNER_QUERY: Final = "?runner_identity="


# The gateway refuses a provider answer cut off by the output budget (finish_reason=length), so a
# budget below what the model needs fails the run. 32 failed c29-20261126T044722Z-f2c5a20f under the
# tenant key; 128 completed (control 275d746c), and 8 or fewer failed the same way.
CHECK_MAX_TOKENS = 128


class GatewayRunSettings(BaseModel):
    """How long one check waits on a run and how often it asks; the node's tested defaults, never an address."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_budget_s: float = Field(default=240.0, gt=0)
    poll_interval_s: float = Field(default=2.0, ge=0)


class GatewayUnreachableError(RuntimeError):
    """A request got no HTTP answer: the gateway or the lab did not answer, so nothing is proven or refuted."""


def resolve_check_route(check_id: CheckId, gateway_url: str) -> ModelCheckRoute:
    """Where the check is graded: the settings store's gateway address, this node as the runner and the omnimarket board."""
    return ModelCheckRoute(
        check_id=check_id,
        kind="gateway"
        if check_id in GATEWAY_ONLY_CHECKS
        else "gateway-and-customer-machine",
        endpoint=gateway_url,
        runner=RUNNER,
        board=BOARD,
    )


def _clause(name: str, ok: bool, detail: str) -> ModelCheckClause:
    return ModelCheckClause(name=name, ok=ok, detail=detail)


def _code_of(body: Any) -> str | None:
    code = body.get("code") if isinstance(body, dict) else None
    return code if isinstance(code, str) and code else None


def _names_missing(body: Any, field: str) -> bool:
    detail = body.get("detail") if isinstance(body, dict) else None
    if not isinstance(detail, list):
        return False
    return any(
        isinstance(e, dict)
        and e.get("type") == "missing"
        and isinstance(e.get("loc"), list)
        and field in e["loc"]
        for e in detail
    )


def _error_fields(body: Any) -> list[str]:
    """The ``field`` of each entry of the ``errors`` list a refused submission carries."""
    errors = body.get("errors") if isinstance(body, dict) else None
    if not isinstance(errors, list):
        return []
    return [
        e["field"]
        for e in errors
        if isinstance(e, dict) and isinstance(e.get("field"), str)
    ]


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, Mapping):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list | tuple):
        return [s for v in value for s in _strings(v)]
    return []


def _house_refs(body: Any) -> list[str]:
    return sorted({s for s in _strings(body) if HOUSE_REF.match(s)})


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


class _Session:
    """One check's requests, all through the one gateway client; an unanswered request ends the check as not measured."""

    def __init__(
        self,
        client: ProtocolGatewayClient,
        settings: GatewayRunSettings,
        check_id: str,
        sleep: Callable[[float], None],
        monotonic: Callable[[], float],
        new_uuid: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        self.client = client
        self.settings = settings
        self.check_id = check_id
        self._sleep = sleep
        self._monotonic = monotonic
        self._new_uuid = new_uuid

    def call(
        self,
        method: str,
        path: str,
        *,
        auth: str,
        body: dict[str, Any] | None = None,
    ) -> ModelGatewayObservation:
        observed = self.client.call(method, path, auth=auth, body=body)
        if observed.status is None:
            raise GatewayUnreachableError(
                f"{method} {path} got no answer: {observed.error or 'no error named'}"
            )
        return observed

    def submit(
        self, task_type: str, source: str, *, auth: str = "tenant"
    ) -> tuple[ModelGatewayObservation, str | None]:
        observed = self.call(
            "POST",
            "/v1/workflows",
            auth=auth,
            body={
                "workflow_type": WORKFLOW_TYPE,
                # The declared request model (ModelWorkflowSubmitRequest) takes a UUID here, never free text.
                "correlation_id": str(self._new_uuid()),
                "payload": {
                    "prompt": f"Reply with the single word: alive. Automated {self.check_id} delegation check.",
                    "task_type": task_type,
                    "source": source,
                    "max_tokens": CHECK_MAX_TOKENS,
                },
            },
        )
        body = observed.body
        workflow_id = _text(body.get("workflow_id")) if isinstance(body, dict) else None
        return observed, workflow_id if observed.status == 202 else None

    def wait(self, workflow_id: str) -> Mapping[str, Any] | None:
        """The status body once it is terminal; the last status body (or None) when the budget ends first."""
        deadline = self._monotonic() + self.settings.run_budget_s
        last: Mapping[str, Any] | None = None
        while True:
            observed = self.call(
                "GET", f"/v1/workflows/{workflow_id}/status", auth="tenant"
            )
            if observed.status == 200 and isinstance(observed.body, dict):
                last = observed.body
                if last.get("status") in TERMINAL:
                    return last
            if self._monotonic() >= deadline:
                return last
            self._sleep(self.settings.poll_interval_s)

    def receipt(self, workflow_id: str) -> Mapping[str, Any] | None:
        observed = self.call(
            "GET",
            f"/v1/workflows/{workflow_id}/receipt{RUNNER_QUERY}{self.check_id.lower()}-probe",
            auth="tenant",
        )
        return (
            observed.body
            if observed.status == 200 and isinstance(observed.body, dict)
            else None
        )

    def run(
        self, task_type: str, source: str
    ) -> tuple[str | None, Mapping[str, Any] | None, Mapping[str, Any] | None, str]:
        """Submit, wait and read the receipt: (workflow id, terminal status body, receipt, what the submit answered)."""
        observed, workflow_id = self.submit(task_type, source)
        answered = f"submit answered {observed.status}"
        if workflow_id is None:
            return None, None, None, answered
        status = self.wait(workflow_id)
        receipt = (
            self.receipt(workflow_id)
            if status is not None and status.get("status") in TERMINAL
            else None
        )
        return workflow_id, status, receipt, answered


def _healthy(
    s: _Session, source: str = "external-client"
) -> tuple[str | None, Mapping[str, Any] | None, Mapping[str, Any] | None, str]:
    return s.run("test", source)


def _identity_clause(name: str, receipt: Mapping[str, Any] | None) -> ModelCheckClause:
    fields = {
        k: _text(receipt.get(k)) if receipt else None
        for k in ("route", "provider", "terminal_model_used")
    }
    missing = [k for k, v in fields.items() if v is None]
    return _clause(
        name,
        receipt is not None and not missing,
        "the receipt names a route, a provider and the model that served"
        if receipt is not None and not missing
        else f"the receipt is missing {missing or 'itself'}",
    )


def _completed(
    name: str, status: Mapping[str, Any] | None, answered: str, workflow_id: str | None
) -> ModelCheckClause:
    state = status.get("status") if status else None
    return _clause(
        name,
        state == "completed",
        f"the run {workflow_id} reads completed"
        if state == "completed"
        else f"the run reads {state!r} ({answered})",
    )


#: The fields the declared status model (ModelWorkflowStatus) and the declared receipt model (ModelWorkflowReceipt) both carry
#: about one terminal run. The status model carries no route or provider (extra="forbid"), so the two bodies are read against
#: each other on what both declare: the same run, the same terminal state, the same model, tokens, latency and failure.
AGREEMENT_FIELDS: Final[tuple[str, ...]] = (
    "workflow_id",
    "correlation_id",
    "workflow_type",
    "status",
    "terminal_model_used",
    "terminal_total_tokens",
    "terminal_latency_ms",
    "terminal_failure_class",
    "terminal_failure_code",
    "terminal_failure_reason",
    "terminal_remediation",
    "rule_evaluations",
    "tests_outcome",
)


def _agreement_clause(
    name: str, status: Mapping[str, Any] | None, receipt: Mapping[str, Any] | None
) -> ModelCheckClause:
    """The status body and the receipt of one terminal run say the same thing on every field both models declare."""
    if status is None or receipt is None:
        return _clause(
            name,
            False,
            "the status or the receipt of the run was not read, so they cannot be compared",
        )
    differ = [f for f in AGREEMENT_FIELDS if status.get(f) != receipt.get(f)]
    return _clause(
        name,
        not differ,
        "the status and the receipt agree on every field both declare"
        if not differ
        else f"the status and the receipt differ on {differ}",
    )


def _c11(s: _Session) -> tuple[ModelCheckClause, ...]:
    wrong_tenant = ABSENT_ID.format(n=11)
    malformed = ABSENT_ID.format(n=12)
    control = s.call("GET", "/health", auth="none")
    cases: tuple[tuple[str, str, str, str, int, str | None, str | None], ...] = (
        (
            "wrong_key",
            "GET",
            "/v1/workflows/contracts",
            "invalid",
            401,
            "auth.credential.invalid",
            None,
        ),
        (
            "absent_key",
            "GET",
            "/v1/workflows/contracts",
            "none",
            401,
            "auth.credential.absent",
            None,
        ),
        (
            "wrong_tenant",
            "GET",
            f"/v1/workflows/{wrong_tenant}/status",
            "tenant",
            404,
            "resource.not_found",
            None,
        ),
        (
            "malformed",
            "GET",
            f"/v1/workflows/{malformed}/receipt",
            "tenant",
            422,
            None,
            "runner_identity",
        ),
    )
    clauses = [
        _clause(
            "positive_control",
            control.status == 200,
            f"the gateway answers /health {control.status}: a dead endpoint refusing everything cannot grade PASS",
        )
    ]
    for name, method, path, auth, status, code, missing in cases:
        got = s.call(method, path, auth=auth)
        ok = got.status == status
        if ok and code is not None:
            ok = _code_of(got.body) == code
        if ok and missing is not None:
            ok = _names_missing(got.body, missing)
        clauses.append(
            _clause(
                name,
                ok,
                f"refused {got.status} {_code_of(got.body) or ''}".strip()
                if ok
                else f"expected {status} {code or missing or ''}, observed {got.status} {_code_of(got.body) or ''}".strip(),
            )
        )
    return tuple(clauses)


def _c12(s: _Session) -> tuple[ModelCheckClause, ...]:
    got = s.call("GET", "/v1/workflows/contracts", auth="tenant")
    body = got.body
    entries = body.get("contracts") if isinstance(body, dict) else body
    entries = entries if isinstance(entries, list) else []
    names = [
        n
        for e in entries
        if isinstance(e, dict)
        for n in [_text(e.get("workflow_type")) or _text(e.get("name"))]
        if n
    ]
    houses = _house_refs(body)
    return (
        _clause(
            "catalogue_served",
            got.status == 200 and bool(names),
            f"the gateway serves {len(names)} contracts"
            if got.status == 200 and names
            else f"contracts answered {got.status} with {len(names)} named entries",
        ),
        _clause(
            "catalogue_unique_and_backs_delegation",
            len(names) == len(set(names)) and WORKFLOW_TYPE in names,
            "each contract is named once and the delegation workflow is among them"
            if len(names) == len(set(names)) and WORKFLOW_TYPE in names
            else "a contract is named twice or the delegation workflow is not served",
        ),
        _clause(
            "no_house_entry",
            not houses,
            "no entry carries a house credential reference"
            if not houses
            else f"{len(houses)} entries carry a house credential reference",
        ),
    )


def _c15(s: _Session) -> tuple[ModelCheckClause, ...]:
    workflow_id, status, receipt, answered = _healthy(s)
    return (
        _clause(
            "submitted",
            workflow_id is not None,
            f"accepted as {workflow_id}" if workflow_id else answered,
        ),
        _completed("completed_within_budget", status, answered, workflow_id),
        _identity_clause("receipt_names_route_provider_model", receipt),
    )


def _rules_clause(receipt: Mapping[str, Any] | None) -> ModelCheckClause:
    rules = receipt.get("rule_evaluations") if receipt else None
    if not isinstance(rules, list) or not rules:
        return _clause(
            "rules_evaluated", False, "a completed run carries no evaluated rule"
        )
    bad = [
        str(r.get("rule", i)) if isinstance(r, dict) else str(i)
        for i, r in enumerate(rules)
        if not isinstance(r, dict)
        or not isinstance(r.get("passed"), bool)
        or (r.get("enforcement") == "blocking" and r["passed"] is not True)
    ]
    return _clause(
        "rules_evaluated",
        not bad,
        f"{len(rules)} rules stated a verdict"
        if not bad
        else f"rules without a boolean verdict or blocking and false: {bad}",
    )


def _dying_clauses(s: _Session, *, prefix: str) -> tuple[ModelCheckClause, ...]:
    workflow_id, status, receipt, answered = s.run(DYING_TASK_TYPE, "external-client")
    state = status.get("status") if status else None
    if workflow_id is None:
        return (
            _clause(
                f"{prefix}", False, f"the dying submission was not accepted: {answered}"
            ),
        )
    if state != "failed":
        return (
            _clause(
                prefix,
                False,
                f"the run reads {state!r}, not failed: a dead run is indistinguishable from one still running",
            ),
        )
    typed = receipt is not None
    problems: list[str] = []
    for key, pattern in (
        ("terminal_failure_class", FAILURE_CLASS),
        ("terminal_failure_code", FAILURE_CODE),
    ):
        value = receipt.get(key) if receipt else None
        if not isinstance(value, str) or not pattern.match(value):
            problems.append(f"{key}={value!r}")
        elif status is None or status.get(key) != value:
            problems.append(f"{key} differs between status and receipt")
    if receipt is not None and receipt.get("status") == "completed":
        problems.append("the receipt reads completed")
    ok = typed and not problems
    return (
        _clause(
            prefix,
            ok,
            "the dead run reads failed with a typed failure class and code"
            if ok
            else f"the dead run carries no typed cause: {problems or 'no receipt'}",
        ),
    )


def _c16(s: _Session) -> tuple[ModelCheckClause, ...]:
    workflow_id, status, receipt, answered = _healthy(s)
    return (
        _completed("healthy_completed", status, answered, workflow_id),
        _identity_clause("receipt_identity_present", receipt),
        _agreement_clause("status_agrees_with_receipt", status, receipt),
        _rules_clause(receipt),
        *_dying_clauses(s, prefix="dead_run_typed_failure"),
    )


def _c28(s: _Session) -> tuple[ModelCheckClause, ...]:
    workflow_id, status, _receipt, answered = _healthy(s)
    return (
        _completed("healthy_consumed", status, answered, workflow_id),
        *_dying_clauses(s, prefix="invalid_payload_observed"),
    )


def _c29(s: _Session) -> tuple[ModelCheckClause, ...]:
    workflow_id, status, receipt, answered = _healthy(s)
    houses = _house_refs(receipt)
    refused, _ = s.submit("test", "external-client", auth="invalid")
    return (
        _completed("completed_under_tenant_key", status, answered, workflow_id),
        _identity_clause("receipt_names_provider_and_model", receipt),
        _clause(
            "no_house_credential_in_receipt",
            receipt is not None and not houses,
            "the receipt names no house credential reference"
            if receipt is not None and not houses
            else "no receipt to read or it names a house credential reference",
        ),
        _clause(
            "house_key_never_stands_in",
            refused.status == 401,
            "a request with a credential no tenant owns is refused 401"
            if refused.status == 401
            else f"a request with a credential no tenant owns answered {refused.status}",
        ),
    )


def _c30(s: _Session) -> tuple[ModelCheckClause, ...]:
    workflow_id, status, receipt, answered = _healthy(s, "claude-code")
    unknown, _ = s.submit("test", UNKNOWN_SOURCE)
    # The gateway answers a payload the workflow contract refuses 400 with the violated fields (handler_workflow_submit.py
    # step 5); 422 is the receipt route's missing query field. The refusal counts when it names the source field.
    refused_on_field = unknown.status == 400 and "source" in _error_fields(unknown.body)
    return (
        _clause(
            "plugin_source_accepted",
            workflow_id is not None,
            "a delegation that enters as the Claude Code plugin is accepted"
            if workflow_id
            else answered,
        ),
        _completed("completed_through_gateway", status, answered, workflow_id),
        _identity_clause("receipt_names_provider_and_model", receipt),
        _clause(
            "unknown_source_refused",
            refused_on_field,
            "a source outside the three adapters is refused 400 naming the source field"
            if refused_on_field
            else f"a source outside the three adapters answered {unknown.status}, "
            f"fields named {_error_fields(unknown.body)}",
        ),
    )


def _c13_leg(s: _Session) -> tuple[ModelCheckClause, ...]:
    workflow_id, status, receipt, answered = _healthy(s)
    return (
        _completed("gateway_delegation_completes", status, answered, workflow_id),
        _identity_clause("gateway_receipt_names_route_provider_model", receipt),
    )


def _c14_leg(s: _Session) -> tuple[ModelCheckClause, ...]:
    workflow_id, status, receipt, answered = _healthy(s)
    return (
        _completed("gateway_routed_run_completes", status, answered, workflow_id),
        _agreement_clause("gateway_status_agrees_with_receipt", status, receipt),
        *_dying_clauses(s, prefix="gateway_unroutable_class_typed_failure"),
    )


def _c17_leg(s: _Session) -> tuple[ModelCheckClause, ...]:
    health = s.call("GET", "/health", auth="none")
    contracts = s.call("GET", "/v1/workflows/contracts", auth="tenant")
    absent = s.call("GET", "/v1/workflows/contracts", auth="none")
    malformed = s.call(
        "GET", f"/v1/workflows/{ABSENT_ID.format(n=17)}/receipt", auth="tenant"
    )
    return (
        _clause(
            "gateway_surface_answers",
            health.status == 200 and contracts.status == 200,
            "the gateway answers /health and serves its contracts to the tenant"
            if health.status == 200 and contracts.status == 200
            else f"health answered {health.status}, contracts answered {contracts.status}",
        ),
        _clause(
            "gateway_refusals_typed",
            absent.status == 401
            and _code_of(absent.body) is not None
            and malformed.status == 422
            and _names_missing(malformed.body, "runner_identity"),
            "a request with no key is refused 401 with a code and a malformed one names the missing field"
            if absent.status == 401 and malformed.status == 422
            else f"no-key answered {absent.status}, malformed answered {malformed.status}",
        ),
    )


_GRADERS: Final[dict[str, Callable[[_Session], tuple[ModelCheckClause, ...]]]] = {
    "C13": _c13_leg,
    "C14": _c14_leg,
    "C17": _c17_leg,
    "C11": _c11,
    "C12": _c12,
    "C15": _c15,
    "C16": _c16,
    "C28": _c28,
    "C29": _c29,
    "C30": _c30,
}
assert set(_GRADERS) == set(GATEWAY_CHECKS)
assert set(get_args(CheckId)) >= set(GATEWAY_CHECKS) | set(CUSTOMER_LOCAL_CHECKS)


def run_gateway_check(
    check_id: CheckId,
    client: ProtocolGatewayClient,
    settings: GatewayRunSettings,
    *,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    new_uuid: Callable[[], uuid.UUID] = uuid.uuid4,
) -> ModelDelegationCheckReading:
    """Grade one check, or the gateway leg of a customer-local check, by requests to ``client`` alone."""
    session = _Session(client, settings, check_id, sleep, monotonic, new_uuid)
    head = {"endpoint": client.gateway_url, "tenant_slug": client.tenant_slug}
    try:
        clauses = _GRADERS[cast("str", check_id)](session)
    except GatewayUnreachableError as exc:
        return ModelDelegationCheckReading(
            **head, not_measured=f"the gateway did not answer: {exc}"
        )
    return ModelDelegationCheckReading(**head, clauses=clauses)


__all__ = [
    "AGREEMENT_FIELDS",
    "CLOUD_DEPENDENT_CHECKS",
    "CUSTOMER_LOCAL_CHECKS",
    "GATEWAY_CHECKS",
    "GATEWAY_ONLY_CHECKS",
    "GatewayRunSettings",
    "GatewayUnreachableError",
    "resolve_check_route",
    "run_gateway_check",
]
