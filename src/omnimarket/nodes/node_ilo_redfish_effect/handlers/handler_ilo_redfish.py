# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""iLO 4 Redfish effect handler: ``handle(request) -> result`` (OMN-21835).

Read verbs run behind a pinned-TLS session and change nothing. A write verb is refused before any
network call unless the cited OPERATOR-CONSENT row names its action and host; it is refused again, after
the live system has been read, unless the live serial and product are the overlay's; it is not sent a
second time when the journal holds an earlier attempt for its key; and it is confirmed by reading state
back. Everything returned is redacted. See the design note ``ilo4-redfish-management-design`` in the
shared knowledge base.

The handler holds no host, address, credential or fingerprint. The overlay, the credential source, the
approval source, the journal, the clock and the transport factory are all injected.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path

import yaml
from pydantic import JsonValue, SecretStr

from omnimarket.nodes.node_ilo_redfish_effect.handlers.journal_file import (
    JournalUnreadableError,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.protocols import (
    Clock,
    CredentialSourceNotStoreError,
    ProtocolActionJournal,
    ProtocolApprovalSource,
    ProtocolCredentialSource,
    RedfishTransportError,
    Sleeper,
    TransportFactory,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.reads import (
    read_discover,
    read_health,
    read_identity_data,
    read_inventory,
    read_logs,
    read_power_status,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.redaction import (
    pipe_free,
    redact_object,
    redact_text,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.redfish_client import (
    IloRefusalError,
    JsonObject,
    ModelIloEndpoints,
    RedfishSession,
    discover,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.safety import (
    check_approval,
    check_identity_for_write,
    idempotency_key,
    read_identity,
)
from omnimarket.nodes.node_ilo_redfish_effect.handlers.writes import (
    PlanRun,
    RunContext,
    WritePlan,
    plan_boot_once,
    plan_power,
    plan_virtual_media,
    run_plan,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.enum_ilo_redfish import (
    EnumIloJournalStatus,
    EnumIloLogKind,
    EnumIloOutcome,
    EnumIloRefusal,
    EnumIloVerb,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_overlay import (
    ModelIloHost,
    ModelIloOverlay,
    credential_key,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_redfish_request import (
    ModelIloRedfishRequest,
)
from omnimarket.nodes.node_ilo_redfish_effect.models.model_ilo_redfish_result import (
    ModelIloEvidenceStep,
    ModelIloRedfishResult,
)

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


def load_credential_key_template(contract_path: Path = CONTRACT_PATH) -> str:
    """The store-key template the node contract declares (``config.credential_key_template.default``)."""
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    template = raw["config"]["credential_key_template"]["default"]
    if not isinstance(template, str):
        raise ValueError("credential_key_template must be a string")
    return template


class HandlerIloRedfish:
    """One request in, one redacted result out. Never raises for a refusal."""

    def __init__(
        self,
        *,
        overlay: ModelIloOverlay,
        transport_factory: TransportFactory,
        credentials: ProtocolCredentialSource,
        approvals: ProtocolApprovalSource,
        journal: ProtocolActionJournal,
        clock: Clock,
        sleeper: Sleeper,
        credential_key_template: str,
        verify_timeout_s: float = 120.0,
        poll_interval_s: float = 5.0,
    ) -> None:
        self._overlay = overlay
        self._transport_factory = transport_factory
        self._credentials = credentials
        self._approvals = approvals
        self._journal = journal
        self._clock = clock
        self._sleeper = sleeper
        self._template = credential_key_template
        self._polls = max(1, math.ceil(verify_timeout_s / poll_interval_s) + 1)
        self._interval = poll_interval_s

    # -- entry ------------------------------------------------------------------------------

    def handle(self, request: ModelIloRedfishRequest) -> ModelIloRedfishResult:
        key = idempotency_key(request) if request.is_write else None
        try:
            return self._handle(request, key)
        except IloRefusalError as exc:
            return self._refused(request, exc.refusal, exc.detail, key)
        except RedfishTransportError as exc:
            refusal = (
                EnumIloRefusal.TLS_FAILED
                if exc.kind == "tls"
                else EnumIloRefusal.CONNECT_FAILED
            )
            return self._refused(request, refusal, exc.kind, key)

    def _handle(
        self, request: ModelIloRedfishRequest, key: str | None
    ) -> ModelIloRedfishResult:
        host = self._overlay.host(request.host)
        if host is None:
            raise IloRefusalError(EnumIloRefusal.HOST_NOT_CONFIGURED)
        if not host.live:
            raise IloRefusalError(EnumIloRefusal.HOST_NOT_LIVE)
        if request.is_write:
            # Before any credential is read and before any connection is opened.
            approval = (
                self._approvals.find(request.approval_ref)
                if request.approval_ref
                else None
            )
            check_approval(approval, request, self._overlay, self._clock())
        username, password = self._account(host, write=request.is_write)

        transport = self._transport_factory(host)
        session = RedfishSession(transport)
        try:
            root = session.read_root()
            session.login(root, username, password)
            endpoints = discover(session, root)
            if request.is_write and key is not None:
                return self._serve_write(session, endpoints, host, request, key)
            return self._serve_read(session, root, endpoints, host, request)
        finally:
            session.logout()

    # -- credentials -----------------------------------------------------------------------

    def _account(
        self, host: ModelIloHost, *, write: bool
    ) -> tuple[SecretStr, SecretStr]:
        role = "WRITE" if write else "READ"
        names = host.credentials
        if names is not None:
            user_key = names.write_username_key if write else names.read_username_key
            pass_key = names.write_password_key if write else names.read_password_key
            if user_key is None or pass_key is None:
                raise IloRefusalError(
                    EnumIloRefusal.CREDENTIAL_MISSING,
                    f"{role.lower()} account not named",
                )
        else:
            user_key = credential_key(
                self._template, host=host.name, role=role, field="USERNAME"
            )
            pass_key = credential_key(
                self._template, host=host.name, role=role, field="PASSWORD"
            )
        try:
            username = self._credentials.get(user_key)
            password = self._credentials.get(pass_key)
        except CredentialSourceNotStoreError:
            raise IloRefusalError(EnumIloRefusal.CREDENTIAL_SOURCE_NOT_STORE) from None
        if username is None or password is None:
            raise IloRefusalError(
                EnumIloRefusal.CREDENTIAL_MISSING,
                f"{role.lower()} account not in the secret store",
            )
        return username, password

    # -- reads -----------------------------------------------------------------------------

    def _serve_read(
        self,
        session: RedfishSession,
        root: JsonObject,
        endpoints: ModelIloEndpoints,
        host: ModelIloHost,
        request: ModelIloRedfishRequest,
    ) -> ModelIloRedfishResult:
        data: dict[str, JsonValue]
        if request.verb is EnumIloVerb.DISCOVER:
            data = read_discover(session, root, endpoints, host)
        elif request.verb is EnumIloVerb.IDENTITY:
            system = session.get(endpoints.system)
            data = read_identity_data(
                read_identity(host, system), session.get(endpoints.manager), host
            )
        elif request.verb is EnumIloVerb.INVENTORY:
            data = read_inventory(session, endpoints)
        elif request.verb is EnumIloVerb.HEALTH:
            data = read_health(session, endpoints)
        elif request.verb is EnumIloVerb.LOGS:
            data = read_logs(
                session,
                endpoints,
                request.log_kind or EnumIloLogKind.IML,
                request.log_limit,
            )
        else:
            # The caller routes only the six read verbs here; the last one is power-status.
            data = read_power_status(session, endpoints)
        return ModelIloRedfishResult(
            verb=request.verb,
            host=request.host,
            outcome=EnumIloOutcome.OK,
            data=redact_object(data),
        )

    # -- writes ----------------------------------------------------------------------------

    def _plan(
        self,
        session: RedfishSession,
        endpoints: ModelIloEndpoints,
        system: JsonObject,
        host: ModelIloHost,
        request: ModelIloRedfishRequest,
    ) -> WritePlan:
        if request.verb is EnumIloVerb.POWER:
            return plan_power(session, endpoints, system, request)
        if request.verb is EnumIloVerb.VIRTUAL_MEDIA:
            return plan_virtual_media(session, endpoints, host, request)
        return plan_boot_once(session, endpoints, system, request)

    def _serve_write(
        self,
        session: RedfishSession,
        endpoints: ModelIloEndpoints,
        host: ModelIloHost,
        request: ModelIloRedfishRequest,
        key: str,
    ) -> ModelIloRedfishResult:
        system = session.get(endpoints.system)
        check_identity_for_write(host, read_identity(host, system))

        try:
            prior = self._journal.lookup(host.name, key)
        except JournalUnreadableError:
            raise IloRefusalError(
                EnumIloRefusal.PRECONDITION_FAILED, "journal unreadable"
            ) from None
        if prior and prior[-1].status is not EnumIloJournalStatus.REJECTED:
            return self._already_attempted(
                session, endpoints, system, host, request, key, prior[-1].status
            )

        plan = self._plan(session, endpoints, system, host, request)
        run = run_plan(
            plan,
            session,
            self._journal,
            RunContext(host=host.name, key=key, action=request.action_label),
            clock=self._clock,
            wait_for=self._wait_for,
        )
        return self._write_result(request, key, run)

    def _wait_for(self, verify: Callable[[], tuple[bool, str]]) -> tuple[bool, str]:
        observed = ""
        for attempt in range(self._polls):
            ok, observed = verify()
            if ok:
                return True, observed
            if attempt + 1 < self._polls:
                self._sleeper(self._interval)
        return False, observed

    def _already_attempted(
        self,
        session: RedfishSession,
        endpoints: ModelIloEndpoints,
        system: JsonObject,
        host: ModelIloHost,
        request: ModelIloRedfishRequest,
        key: str,
        last: EnumIloJournalStatus,
    ) -> ModelIloRedfishResult:
        try:
            live = self._plan(session, endpoints, system, host, request).found
        except IloRefusalError as exc:
            live = f"state not plannable: {exc.refusal.value}"
        run = PlanRun(
            outcome=EnumIloOutcome.ALREADY_ATTEMPTED,
            detail=f"an earlier attempt for this approval is journalled as {last.value}; nothing was sent again",
            evidence=(
                ModelIloEvidenceStep(
                    step="journal", observed=f"last status {last.value}"
                ),
                ModelIloEvidenceStep(step="live", observed=live),
            ),
        )
        return self._write_result(request, key, run)

    def _write_result(
        self, request: ModelIloRedfishRequest, key: str, run: PlanRun
    ) -> ModelIloRedfishResult:
        evidence = tuple(
            ModelIloEvidenceStep(step=e.step, observed=redact_text(e.observed))
            for e in run.evidence
        )
        detail = redact_text(run.detail) if run.detail else None
        return ModelIloRedfishResult(
            verb=request.verb,
            host=request.host,
            outcome=run.outcome,
            refusal=run.refusal,
            detail=detail,
            evidence=evidence,
            idempotency_key=key,
            ledger_row=self._ledger_row(
                request, key, run.outcome, run.refusal, detail, evidence
            ),
        )

    # -- results ---------------------------------------------------------------------------

    def _refused(
        self,
        request: ModelIloRedfishRequest,
        refusal: EnumIloRefusal,
        detail: str | None,
        key: str | None,
    ) -> ModelIloRedfishResult:
        clean = redact_text(detail) if detail else None
        row = (
            self._ledger_row(request, key, EnumIloOutcome.REFUSED, refusal, clean, ())
            if request.is_write and key is not None
            else None
        )
        return ModelIloRedfishResult(
            verb=request.verb,
            host=request.host,
            outcome=EnumIloOutcome.REFUSED,
            refusal=refusal,
            detail=clean,
            idempotency_key=key,
            ledger_row=row,
        )

    def _ledger_row(
        self,
        request: ModelIloRedfishRequest,
        key: str | None,
        outcome: EnumIloOutcome,
        refusal: EnumIloRefusal | None,
        detail: str | None,
        evidence: tuple[ModelIloEvidenceStep, ...],
    ) -> str:
        """One pipe-free line for the caller to append to the work ledger with the ledger-write skill."""
        evidence_text = "; ".join(f"{e.step}={e.observed}" for e in evidence) or "none"
        parts = [
            f"ilo-redfish {request.action_label} host={request.host}",
            f"requested_by={request.requested_by}",
            f"approval={request.approval_ref or 'none'}",
            f"key={(key or 'none')[:16]}",
            f"outcome={outcome.value}",
            f"refusal={refusal.value if refusal else 'none'}",
            f"detail={detail or 'none'}",
            f"evidence=[{evidence_text}]",
        ]
        return pipe_free(redact_text(" ".join(parts)))


__all__ = [
    "CONTRACT_PATH",
    "HandlerIloRedfish",
    "load_credential_key_template",
]
