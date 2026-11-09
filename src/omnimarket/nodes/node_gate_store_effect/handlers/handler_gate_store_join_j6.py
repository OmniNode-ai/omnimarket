# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J6 (piece 6 to piece 7): a stop committed in the gate store is audited and fences the piece's sends (OMN-22936).

Canonical shape: ``handle(ModelJoinJ6Request) -> ModelJoinJ6Result``.

* ``stop``: close the send fence for the piece first, then commit the stop in the gate store under a deadline. An answer
  of ok (or a replay of the same step key) writes one durable audit row and projects it, and the result reads success
  only when both rows read back. No answer by the deadline, or a store that raises, writes an ``unanswered`` audit row and
  leaves the fence closed: the fence fails closed, never open.
* ``send``: a piece whose stop is not confirmed is refused at the fence without asking the store; a piece whose stop is
  committed is asked of the store, which refuses it; any other piece is reserved in the store as usual.

Everything it calls is injected: the gate store, the audit sink and the projection. Its settings are the contract's
``join_j6`` section.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.model_gate_store import (
    EnumGateStoreOperation,
    EnumGateStoreOutcome,
    ModelGateStoreRequest,
    ModelGateStoreResult,
)
from omnimarket.models.model_gate_store_join_j6 import (
    EnumJoinJ6Operation,
    EnumJoinJ6Outcome,
    ModelJoinJ6AuditRow,
    ModelJoinJ6Policy,
    ModelJoinJ6Request,
    ModelJoinJ6Result,
)
from omnimarket.protocols.protocol_gate_store import ProtocolGateStore
from omnimarket.protocols.protocol_gate_store_join_j6 import (
    ProtocolJoinJ6AuditSink,
    ProtocolJoinJ6Projection,
)

CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"
_SECTION = "join_j6"
_COMMITTED = frozenset({EnumGateStoreOutcome.OK, EnumGateStoreOutcome.DUPLICATE})
_FENCE_COMMITTED = "committed"
_FENCE_OPEN_QUESTION = "unconfirmed"


def load_join_j6_policy(contract: Mapping[str, Any] | None = None) -> ModelJoinJ6Policy:
    """The contract's ``join_j6`` section; an absent section or key refuses naming it."""
    if contract is None:
        contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    section = contract.get(_SECTION)
    if not isinstance(section, Mapping):
        raise ValueError(f"contract has no {_SECTION} section")
    kinds = section["audit_kinds"]
    codes = section["failure_codes"]
    return ModelJoinJ6Policy(
        join_id=section["join_id"],
        from_piece=section["from_piece"],
        to_piece=section["to_piece"],
        commit_deadline_seconds=section["commit_deadline_seconds"],
        audit_kind_committed=kinds["committed"],
        audit_kind_unanswered=kinds["unanswered"],
        audit_kind_refused=kinds["refused"],
        audit_row_missing_failure_code=codes["audit_row_missing"],
        projected_row_missing_failure_code=codes["projected_row_missing"],
    )


class HandlerGateStoreJoinJ6:
    """One request, one answer; the fence is held here and is only ever closed by a stop."""

    def __init__(
        self,
        store: ProtocolGateStore,
        audit: ProtocolJoinJ6AuditSink,
        projection: ProtocolJoinJ6Projection,
        policy: ModelJoinJ6Policy,
    ) -> None:
        self._store = store
        self._audit = audit
        self._projection = projection
        self._policy = policy
        self._lock = threading.Lock()
        self._fence: dict[str, str] = {}

    def handle(self, request: ModelJoinJ6Request) -> ModelJoinJ6Result:
        """Commit a stop or admit a send, and answer typed."""
        if request.operation is EnumJoinJ6Operation.STOP:
            return self._stop(request)
        return self._send(request)

    def _stop(self, request: ModelJoinJ6Request) -> ModelJoinJ6Result:
        with self._lock:
            self._fence.setdefault(request.piece, _FENCE_OPEN_QUESTION)
        answer, detail = self._commit(request)
        if answer is None:
            kind, outcome = (
                self._policy.audit_kind_unanswered,
                (EnumJoinJ6Outcome.STOP_UNANSWERED),
            )
        elif answer.outcome in _COMMITTED:
            kind, outcome = (
                self._policy.audit_kind_committed,
                (EnumJoinJ6Outcome.STOP_COMMITTED),
            )
            with self._lock:
                self._fence[request.piece] = _FENCE_COMMITTED
        else:
            kind, outcome = (
                self._policy.audit_kind_refused,
                (EnumJoinJ6Outcome.STOP_REFUSED),
            )
            detail = f"store answered {answer.outcome.value}"
        row = ModelJoinJ6AuditRow(
            row_id=request.step_key,
            correlation_id=request.correlation_id,
            piece=request.piece,
            kind=kind,
            epoch=None if answer is None else answer.epoch,
        )
        self._audit.append_if_absent(row)
        durable = self._audit.read(row.row_id)
        if durable is None:
            return self._failed(
                request,
                self._policy.audit_row_missing_failure_code,
                answer,
                "the audit row does not read back from the durable sink",
            )
        self._projection.project(durable)
        if self._projection.read(row.row_id) is None:
            return self._failed(
                request,
                self._policy.projected_row_missing_failure_code,
                answer,
                "the audit row does not read back from the projection",
            )
        return ModelJoinJ6Result(
            correlation_id=request.correlation_id,
            operation=request.operation,
            piece=request.piece,
            outcome=outcome,
            store_outcome=None if answer is None else answer.outcome.value,
            audit_row_id=row.row_id,
            detail=detail,
        )

    def _commit(
        self, request: ModelJoinJ6Request
    ) -> tuple[ModelGateStoreResult | None, str]:
        """The stop's commit under the deadline; ``None`` when nothing answered in time."""
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            self._store.transact,
            ModelGateStoreRequest(
                correlation_id=request.correlation_id,
                operation=EnumGateStoreOperation.RECORD_SAFETY,
                step_key=request.step_key,
                now=request.now,
                piece=request.piece,
                safety_state="stop",
            ),
        )
        try:
            return future.result(timeout=self._policy.commit_deadline_seconds), ""
        except FutureTimeoutError:
            return None, (
                f"no answer within {self._policy.commit_deadline_seconds} seconds"
            )
        except (
            Exception
        ) as exc:  # the store's own fault is no answer, and the fence stays closed
            return None, f"store fault {type(exc).__name__}"
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _send(self, request: ModelJoinJ6Request) -> ModelJoinJ6Result:
        with self._lock:
            fence = self._fence.get(request.piece)
        if fence == _FENCE_OPEN_QUESTION:
            return ModelJoinJ6Result(
                correlation_id=request.correlation_id,
                operation=request.operation,
                piece=request.piece,
                outcome=EnumJoinJ6Outcome.SEND_REFUSED,
                detail="the fence is closed: the stop of this piece is not confirmed",
            )
        answer = self._store.transact(
            ModelGateStoreRequest(
                correlation_id=request.correlation_id,
                operation=EnumGateStoreOperation.RESERVE,
                step_key=request.step_key,
                now=request.now,
                piece=request.piece,
                reservation_id=request.reservation_id,
            )
        )
        admitted = answer.outcome is EnumGateStoreOutcome.OK
        return ModelJoinJ6Result(
            correlation_id=request.correlation_id,
            operation=request.operation,
            piece=request.piece,
            outcome=(
                EnumJoinJ6Outcome.SEND_ADMITTED
                if admitted
                else EnumJoinJ6Outcome.SEND_REFUSED
            ),
            store_outcome=answer.outcome.value,
        )

    def _failed(
        self,
        request: ModelJoinJ6Request,
        code: str,
        answer: ModelGateStoreResult | None,
        detail: str,
    ) -> ModelJoinJ6Result:
        return ModelJoinJ6Result(
            correlation_id=request.correlation_id,
            operation=request.operation,
            piece=request.piece,
            outcome=EnumJoinJ6Outcome.FAILED,
            store_outcome=None if answer is None else answer.outcome.value,
            failure_code=code,
            detail=detail,
        )
