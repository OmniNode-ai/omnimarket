# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The durable goal tick's read-only S7/S9 checks and exception outbox."""

from dataclasses import asdict
from pathlib import Path
from typing import Any

from omnimarket.handlers.container.container_overlay_dependencies import (
    declared_handler_dependencies,
    overlay_documents,
    packaged_contract_value,
    require_dependencies,
)
from omnimarket.models.exception_queue import (
    ModelExceptionEmitRequest,
    ModelExceptionRow,
)
from omnimarket.models.host_checks import (
    ModelHostAccountRequest,
    ModelHostCheckCursor,
    ModelHostCheckEvent,
    ModelHostCheckFinding,
    ModelHostCheckPolicy,
    ModelHostCheckTick,
    ModelHostCheckTickResult,
    ModelHostFact,
    ModelInstallClaimRequest,
)
from omnimarket.models.liveness.model_automation_liveness import (
    ModelAutomationRunObserved,
)
from omnimarket.nodes.node_host_account_check_compute import HandlerHostAccountCheck
from omnimarket.nodes.node_install_claim_check_compute import HandlerInstallClaimCheck
from omnimarket.protocols.protocol_host_checks import (
    ProtocolHostCheckEvents,
    ProtocolHostCheckFacts,
    ProtocolHostCheckJournal,
)
from omnimarket.protocols.protocol_overlay_document import ProtocolOverlayDocument

PACKAGE = "omnimarket.nodes.node_host_account_install_claim_effect"


class HandlerHostAccountInstallClaim:
    def __init__(
        self,
        *,
        policy: ModelHostCheckPolicy | None = None,
        facts: ProtocolHostCheckFacts | None = None,
        journal: ProtocolHostCheckJournal | None = None,
        events: ProtocolHostCheckEvents | None = None,
    ) -> None:
        self._policy, self._facts, self._journal, self._events = (
            policy,
            facts,
            journal,
            events,
        )
        self._documents: ProtocolOverlayDocument | None = None

    def _bind(self) -> None:
        if self._documents is not None:
            self._policy = ModelHostCheckPolicy.model_validate(
                self._documents.read("host_checks")
            )
        if all(
            x is not None
            for x in (self._policy, self._facts, self._journal, self._events)
        ):
            return
        dependencies = require_dependencies(declared_handler_dependencies(PACKAGE))
        facts = dependencies[
            f"{ProtocolHostCheckFacts.__module__}.{ProtocolHostCheckFacts.__name__}"
        ]
        journal = dependencies[
            f"{ProtocolHostCheckJournal.__module__}.{ProtocolHostCheckJournal.__name__}"
        ]
        events = dependencies[
            f"{ProtocolHostCheckEvents.__module__}.{ProtocolHostCheckEvents.__name__}"
        ]
        if not isinstance(facts, ProtocolHostCheckFacts):
            raise TypeError("host facts port does not satisfy its protocol")
        if not isinstance(journal, ProtocolHostCheckJournal):
            raise TypeError("host journal port does not satisfy its protocol")
        if not isinstance(events, ProtocolHostCheckEvents):
            raise TypeError("host event port does not satisfy its protocol")
        self._facts, self._journal, self._events = facts, journal, events
        self._documents = overlay_documents(dependencies)
        self._policy = ModelHostCheckPolicy.model_validate(
            self._documents.read("host_checks")
        )

    def handle(self, request: ModelHostCheckTick) -> ModelHostCheckTickResult:
        try:
            self._bind()
        except (ValueError, OSError, TypeError, KeyError):
            # Binding errors contain configuration and are deliberately not echoed.
            unread = self._unread(
                request, "R8: host check overlay or dependency unreadable"
            )
            return (
                self._journal.record(unread, ())
                if self._journal is not None
                else unread
            )
        assert self._journal is not None
        assert self._facts is not None
        assert self._policy is not None
        assert self._events is not None
        if set(self._policy.liveness_entries) != {"S7", "S9"}:
            return self._journal.record(
                self._unread(
                    request, "R8: both host-check liveness entries are required"
                ),
                (),
            )
        if self._events.principal != self._policy.check_principal:
            return self._journal.record(
                self._unread(
                    request,
                    "R8: goal check bus principal does not match its service binding",
                ),
                (),
            )
        prior = self._journal.result(request.tick_id)
        if prior is not None:
            return self._deliver(prior)
        try:
            snapshot = self._facts.read(self._policy)
        except (OSError, ValueError, TypeError):
            result = self._unread(request, "R8: host facts unreadable")
        else:
            if snapshot.prerequisite_error:
                result = self._unread(request, snapshot.prerequisite_error)
            else:
                hosts = {h.host: h for h in snapshot.hosts}
                findings: list[ModelHostCheckFinding] = []
                for binding in self._policy.hosts:
                    findings.append(
                        HandlerHostAccountCheck().handle(
                            ModelHostAccountRequest(
                                host=hosts.get(
                                    binding.host, ModelHostFact(host=binding.host)
                                ),
                                pool_accounts=self._policy.pool_accounts,
                                token_switch_fix=self._policy.token_switch_fix,
                            )
                        )
                    )
                for claim in snapshot.claims:
                    findings.append(
                        HandlerInstallClaimCheck().handle(
                            ModelInstallClaimRequest(
                                claim=claim,
                                host=hosts.get(claim.host),
                                install_principals=self._policy.install_principals,
                                fix=self._policy.install_claim_fix,
                            )
                        )
                    )
                if not snapshot.ledger_readable:
                    findings.append(
                        ModelHostCheckFinding(
                            check="S9",
                            host="",
                            verdict="UNREAD",
                            reason="install-claims-ledger-unread",
                            key="S9:ledger",
                        )
                    )
                cursors = tuple(
                    ModelHostCheckCursor(
                        check=check,
                        tick_id=request.tick_id,
                        at=request.at,
                        verdict=(
                            "UNREAD"
                            if any(
                                f.verdict == "UNREAD"
                                for f in findings
                                if f.check == check
                            )
                            else "FINDING"
                            if any(
                                f.verdict == "FINDING"
                                for f in findings
                                if f.check == check
                            )
                            else "OK"
                        ),
                        rows_read=sum(f.check == check for f in findings),
                    )
                    for check in ("S7", "S9")
                )
                result = ModelHostCheckTickResult(
                    tick_id=request.tick_id, findings=tuple(findings), cursors=cursors
                )
        outbox = self._outbox(result, request)
        result = self._journal.record(result, outbox)
        return self._deliver(result)

    def _unread(
        self, request: ModelHostCheckTick, error: str
    ) -> ModelHostCheckTickResult:
        return ModelHostCheckTickResult(
            tick_id=request.tick_id,
            findings=(),
            error=error,
            cursors=tuple(
                ModelHostCheckCursor(
                    check=check,
                    tick_id=request.tick_id,
                    at=request.at,
                    verdict="UNREAD",
                    rows_read=0,
                )
                for check in ("S7", "S9")
            ),
        )

    def _outbox(
        self, result: ModelHostCheckTickResult, request: ModelHostCheckTick
    ) -> tuple[ModelHostCheckEvent, ...]:
        assert self._policy is not None
        topics = packaged_contract_value(PACKAGE, "host_check_topics")
        events: list[ModelHostCheckEvent] = []
        for finding in result.findings:
            if finding.verdict != "FINDING":
                continue
            key = "exception:" + finding.key
            emit = ModelExceptionEmitRequest(
                state_dir=Path(self._policy.exception_state_dir),
                now=request.at.isoformat(),
                idempotency_key=key,
                row=ModelExceptionRow(
                    cls=finding.reason,
                    key=finding.key,
                    source=self._policy.check_principal,
                    evidence=f"check={finding.check} host={finding.host} label={finding.account_label or ''} claim={finding.claim_id or ''}",
                    tried="read host facts; no repair attempted",
                    proposed_fix=finding.fix,
                ),
            )
            payload: dict[str, Any] = asdict(emit)
            payload["state_dir"] = str(emit.state_dir)
            events.append(
                ModelHostCheckEvent(key=key, topic=topics["exception"], payload=payload)
            )
        for cursor in result.cursors:
            events.append(
                ModelHostCheckEvent(
                    key=f"cursor:{cursor.check}:{request.tick_id}",
                    topic=topics["cursor"],
                    payload=cursor.model_dump(mode="json"),
                )
            )
            entry = self._policy.liveness_entries[cursor.check]
            run = ModelAutomationRunObserved.model_validate(
                {
                    "process_id": entry.process_id,
                    "host": entry.host,
                    "run_id": f"{entry.process_id}:{request.tick_id}",
                    "phase": "finished",
                    "started_at": request.at,
                    "finished_at": request.at,
                    "observed_at": request.at,
                    "outcome": "failed" if cursor.verdict == "UNREAD" else "ok",
                    "exit_code": 1 if cursor.verdict == "UNREAD" else 0,
                    "did_work_count": cursor.rows_read,
                    "evidence_ref": f"cursor:{cursor.check}:{request.tick_id}",
                    "emitter": "self",
                    "contract_digest": entry.digest(),
                }
            )
            events.append(
                ModelHostCheckEvent(
                    key=f"liveness:{cursor.check}:{request.tick_id}",
                    topic=topics["liveness"],
                    payload=run.model_dump(mode="json"),
                )
            )
        return tuple(events)

    def _deliver(self, result: ModelHostCheckTickResult) -> ModelHostCheckTickResult:
        assert self._journal is not None
        assert self._events is not None
        failed = False
        for event in self._journal.pending():
            try:
                self._events.publish(event)
            except (OSError, RuntimeError):
                failed = True
                continue
            self._journal.delivered(event.key)
        return result.model_copy(
            update={
                "pending_events": len(self._journal.pending()),
                "error": result.error
                or ("host-check outbox delivery pending" if failed else ""),
            }
        )
