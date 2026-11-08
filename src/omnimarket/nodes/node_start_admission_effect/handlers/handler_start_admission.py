# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The start admission effect: ``handle(ModelStartAdmissionRequest) -> ModelStartAdmissionResult``.

``admit`` reserves on the injected gate store (one reservation per start, keyed
``<path>/admit/<start_id>``); ``start`` consumes it, keyed per attempt, so a reservation starts one
engine call and a second attempt, or a start that never reserved, is refused by the store's own
compare. A refusal is told to the path only where the overlay puts it in enforce; in observe the
path is admitted and the log keeps what would have been refused. A store that gives no answer
admits and says so (plan 7.1: no reply by the deadline admits, logged). Every decision is logged.
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from pydantic import ValidationError

from omnimarket.models.model_gate_store import (
    EnumGateStoreOperation,
    EnumGateStoreOutcome,
    ModelGateStoreRequest,
    ModelGateStoreResult,
)
from omnimarket.models.model_start_admission import (
    EnumAdmissionDecision,
    EnumAdmissionLogBackend,
    EnumAdmissionOperation,
    EnumEnforcement,
    ModelAdmissionLogEntry,
    ModelStartAdmissionFailure,
    ModelStartAdmissionPolicy,
    ModelStartAdmissionRequest,
    ModelStartAdmissionResult,
)
from omnimarket.protocols.protocol_admission_log import ProtocolAdmissionLog
from omnimarket.protocols.protocol_gate_store import ProtocolGateStore

CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"
DEC = EnumAdmissionDecision
_OK = {EnumGateStoreOutcome.OK}


class AdmissionLogNotBuiltError(RuntimeError):
    """The overlay selected a log backend that is not built; nothing answers in its place."""


class InMemoryAdmissionLog:
    """The log the tests and the first install use; ``entries`` is what was kept."""

    def __init__(self) -> None:
        self.entries: list[ModelAdmissionLogEntry] = []

    def record(self, entry: ModelAdmissionLogEntry) -> None:
        self.entries.append(entry)


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def build_log(policy: ModelStartAdmissionPolicy) -> ProtocolAdmissionLog:
    """The log the overlay names; refuses one that is not built."""
    if policy.log_backend is EnumAdmissionLogBackend.IN_MEMORY:
        return InMemoryAdmissionLog()
    raise AdmissionLogNotBuiltError(
        f"admission log backend {policy.log_backend.value} is not built"
    )


class HandlerStartAdmission:
    """One call on the injected gate store, policy and log."""

    def __init__(
        self,
        store: ProtocolGateStore,
        policy: ModelStartAdmissionPolicy,
        log: ProtocolAdmissionLog,
    ) -> None:
        self._store = store
        self._policy = policy
        self._log = log

    def _store_request(
        self, request: ModelStartAdmissionRequest
    ) -> ModelGateStoreRequest:
        if request.operation is EnumAdmissionOperation.ADMIT:
            return ModelGateStoreRequest(
                correlation_id=request.correlation_id,
                operation=EnumGateStoreOperation.RESERVE,
                step_key=f"{request.path}/admit/{request.start_id}",
                now=request.now,
                piece=request.piece,
                reservation_id=request.start_id,
            )
        return ModelGateStoreRequest(
            correlation_id=request.correlation_id,
            operation=EnumGateStoreOperation.CONSUME,
            step_key=f"{request.path}/start/{request.start_id}/{request.correlation_id}",
            now=request.now,
            reservation_id=request.start_id,
        )

    @staticmethod
    def _accepted(answer: ModelGateStoreResult) -> bool:
        if answer.outcome in _OK:
            return True
        # a redelivered command returns the first answer; it counts only if that was an accept
        return (
            answer.outcome is EnumGateStoreOutcome.DUPLICATE
            and answer.detail.endswith(f"first answer {EnumGateStoreOutcome.OK.value}")
        )

    def handle(self, request: ModelStartAdmissionRequest) -> ModelStartAdmissionResult:
        """Ask the store, decide by the path's mode, log, and tell the path."""
        mode = self._policy.enforcement.get(request.path)
        listed = mode is not None
        mode = mode or EnumEnforcement.OBSERVE
        store_outcome: str | None
        try:
            answer = self._store.transact(self._store_request(request))
        except Exception as exc:  # no answer: admit, say so, log
            decision, would_refuse = DEC.ADMITTED_NO_ANSWER, False
            store_outcome = None
            detail = f"no answer from the store ({type(exc).__name__}): admitted"
        else:
            store_outcome = answer.outcome.value
            would_refuse = not self._accepted(answer)
            detail = answer.detail
            if not would_refuse:
                decision = DEC.ADMITTED
            elif mode is EnumEnforcement.ENFORCE:
                decision = DEC.REFUSED
            else:
                decision = DEC.ADMITTED_OBSERVED
        result = ModelStartAdmissionResult(
            correlation_id=request.correlation_id,
            operation=request.operation,
            decision=decision,
            path=request.path,
            start_id=request.start_id,
            enforcement=mode,
            path_listed=listed,
            would_refuse=would_refuse,
            store_outcome=store_outcome,
            detail=detail,
        )
        self._log.record(
            ModelAdmissionLogEntry(
                correlation_id=request.correlation_id,
                operation=request.operation,
                path=request.path,
                start_id=request.start_id,
                decision=decision,
                would_refuse=would_refuse,
                store_outcome=store_outcome,
                detail=detail,
            )
        )
        return result


def answer_command(
    payload: bytes, handler: HandlerStartAdmission, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One admission command in, the event it produces out, as (topic, JSON bytes).

    A payload that is not a request, or a log that cannot keep its entry, is a typed failure event
    naming the failure, never dropped and never retried silently.
    """
    try:
        request = ModelStartAdmissionRequest.model_validate_json(payload)
    except ValidationError as exc:
        fields = sorted(
            {".".join(str(p) for p in e["loc"]) or "<document>" for e in exc.errors()}
        )
        failure = ModelStartAdmissionFailure(
            correlation_id="unknown", failure=f"bad-payload:{fields}"
        )
        return [(topics["admission_failed"], failure.model_dump_json().encode())]
    try:
        result = handler.handle(request)
    except Exception as exc:  # the log's own fault is an answer, not a crash
        failure = ModelStartAdmissionFailure(
            correlation_id=request.correlation_id,
            failure=f"admission-fault:{type(exc).__name__}",
        )
        return [(topics["admission_failed"], failure.model_dump_json().encode())]
    return [
        (
            topics["admission_decided"],
            json.dumps(result.model_dump(mode="json"), sort_keys=True).encode(),
        )
    ]
