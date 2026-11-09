# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The start-admission effect: ``handle(ModelStartAdmissionRequest) -> ModelStartAdmissionResult`` (OMN-22939).

One request is decided in the gate store and logged:

* reserve the piece; a store refusal is what the policy would refuse (a limit, a stop, a replayed step key).
* consume the reservation at the epoch the reserve answered. The store refuses a consume once the epoch has
  advanced, so a reservation is never consumed after an advance; the effect then releases it and re-decides with a
  fresh reservation, up to the policy's number of re-decisions, and a decision that never settles reads as a refusal.
* ``observe`` mode logs the refusal and the start proceeds; ``enforce`` mode refuses it.
* a store that gives no answer by the deadline (or raises) is no answer: the start proceeds logged as unadmitted, and an
  outward action is not proceeded with, in either mode.

The gate store and the log are injected; the mode of each path and the deadline are the ``start_admission`` overlay.
Nothing here asks a path to start: it answers, and the path acts on ``proceed``.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from enum import StrEnum
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
    EnumAdmissionMode,
    EnumAdmissionOutcome,
    ModelStartAdmissionFailure,
    ModelStartAdmissionLogRow,
    ModelStartAdmissionPolicy,
    ModelStartAdmissionRequest,
    ModelStartAdmissionResult,
)
from omnimarket.protocols.protocol_gate_store import ProtocolGateStore
from omnimarket.protocols.protocol_start_admission import ProtocolStartAdmissionLog

CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"
REASON_REDECISION_EXHAUSTED = "redecision_exhausted"
REASON_NO_ANSWER = "no_answer"


class _NoAnswerError(Exception):
    """The store did not answer in time, or raised; the reason is for the log."""


class _Verdict(StrEnum):
    ADMITTED = "admitted"
    REFUSED = "refused"
    NO_ANSWER = "no_answer"


@dataclass(frozen=True)
class _Decision:
    verdict: _Verdict
    reason: str
    attempts: int
    epoch: int | None


class HandlerStartAdmission:
    """One request, one decision, one log row."""

    def __init__(
        self,
        store: ProtocolGateStore,
        log: ProtocolStartAdmissionLog,
        policy: ModelStartAdmissionPolicy,
    ) -> None:
        self._store = store
        self._log = log
        self._policy = policy

    def handle(self, request: ModelStartAdmissionRequest) -> ModelStartAdmissionResult:
        """Decide the start, log what was decided, and answer typed."""
        mode = self._policy.modes.get(request.path, self._policy.default_mode)
        decision = self._decide(
            request, time.monotonic() + self._policy.answer_deadline_seconds
        )
        if decision.verdict is _Verdict.ADMITTED:
            outcome, proceed = EnumAdmissionOutcome.ADMITTED, True
        elif decision.verdict is _Verdict.NO_ANSWER:
            if request.outward:
                outcome, proceed = EnumAdmissionOutcome.NOT_PROCEEDED, False
            else:
                outcome, proceed = EnumAdmissionOutcome.UNADMITTED, True
        elif mode is EnumAdmissionMode.OBSERVE:
            outcome, proceed = EnumAdmissionOutcome.OBSERVED_REFUSAL, True
        else:
            outcome, proceed = EnumAdmissionOutcome.REFUSED, False
        self._log.append(
            ModelStartAdmissionLogRow(
                correlation_id=request.correlation_id,
                step_key=request.step_key,
                path=request.path,
                piece=request.piece,
                mode=mode,
                outcome=outcome,
                proceed=proceed,
                reason=decision.reason,
                epoch=decision.epoch,
                attempts=decision.attempts,
            )
        )
        return ModelStartAdmissionResult(
            correlation_id=request.correlation_id,
            path=request.path,
            mode=mode,
            outcome=outcome,
            proceed=proceed,
            reason=decision.reason,
            epoch=decision.epoch,
            attempts=decision.attempts,
        )

    def _decide(
        self, request: ModelStartAdmissionRequest, deadline_at: float
    ) -> _Decision:
        attempt = 0
        epoch: int | None = None
        while True:
            attempt += 1
            try:
                reserved = self._transact(
                    self._store_request(request, "reserve", attempt), deadline_at
                )
                epoch = reserved.epoch
                if reserved.outcome is not EnumGateStoreOutcome.OK:
                    return _Decision(
                        _Verdict.REFUSED, reserved.outcome.value, attempt, epoch
                    )
                consumed = self._transact(
                    self._store_request(request, "consume", attempt, epoch),
                    deadline_at,
                )
                epoch = consumed.epoch
                if consumed.outcome is EnumGateStoreOutcome.OK:
                    return _Decision(_Verdict.ADMITTED, "", attempt, epoch)
                released = self._transact(
                    self._store_request(request, "release", attempt), deadline_at
                )
                epoch = released.epoch
                if consumed.outcome is not EnumGateStoreOutcome.REFUSED_FENCE:
                    return _Decision(
                        _Verdict.REFUSED, consumed.outcome.value, attempt, epoch
                    )
            except _NoAnswerError as exc:
                return _Decision(_Verdict.NO_ANSWER, str(exc), attempt, epoch)
            if attempt > self._policy.max_redecisions:
                return _Decision(
                    _Verdict.REFUSED, REASON_REDECISION_EXHAUSTED, attempt, epoch
                )

    @staticmethod
    def _store_request(
        request: ModelStartAdmissionRequest,
        verb: str,
        attempt: int,
        epoch: int | None = None,
    ) -> ModelGateStoreRequest:
        return ModelGateStoreRequest(
            correlation_id=request.correlation_id,
            operation=EnumGateStoreOperation(verb),
            step_key=f"{request.step_key}/{verb}/{attempt}",
            now=request.now,
            piece=request.piece if verb == "reserve" else None,
            reservation_id=request.reservation_id,
            expected_epoch=epoch,
        )

    def _transact(
        self, store_request: ModelGateStoreRequest, deadline_at: float
    ) -> ModelGateStoreResult:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise _NoAnswerError(REASON_NO_ANSWER)
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self._store.transact, store_request)
        try:
            return future.result(timeout=remaining)
        except FutureTimeoutError:
            raise _NoAnswerError(REASON_NO_ANSWER) from None
        except Exception as exc:  # the store's own fault is no answer
            raise _NoAnswerError(f"store_fault:{type(exc).__name__}") from None
        finally:
            executor.shutdown(wait=False, cancel_futures=True)


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def answer_command(
    payload: bytes, handler: HandlerStartAdmission, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    """One command message in, the event it produces out, as (topic, JSON bytes).

    A payload that is no request is answered with a typed failure event, never dropped.
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
        return [(topics["start_admission_failed"], failure.model_dump_json().encode())]
    result = handler.handle(request)
    return [
        (
            topics["start_admission_answered"],
            json.dumps(result.model_dump(mode="json"), sort_keys=True).encode(),
        )
    ]
