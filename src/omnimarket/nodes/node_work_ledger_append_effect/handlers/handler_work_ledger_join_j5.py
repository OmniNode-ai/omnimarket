# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J5 (piece 5 to piece 6): an append under a store-resolved identity lands as a durable row and a projected row (OMN-22914).

Canonical shape: ``handle(ModelJoinJ5Request) -> ModelJoinJ5Result``. The handler answers piece 5 on the floor's
emission contract, under the correlation id piece 5's chain carries:

* the store gives no identity: a typed ``upstream_`` failure that never started, and no row anywhere;
* the identity resolves: a start, the append under that principal, then the row read back from the projection at the
  receipt's position; a success only when both the durable row and the projected row are there, and a typed failure
  with the start before it when the append is not accepted, when the projection never heard the row, or when the
  projection could not be read at all (the failure carries the read error the row source gave).

Everything it calls is injected: the append handler, the ledger reader, the store of identities, the projection, the
emission sink and the clock. Its settings are the contract's ``join_j5`` section.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from importlib import resources
from typing import Any, Literal

import yaml

from omnimarket.handlers.emission_sink import ChainEmitter, ProtocolEmissionSink
from omnimarket.models.work_ledger_append import (
    EnumJoinJ5Outcome,
    EnumWorkLedgerAppendStatus,
    ModelJoinJ5Policy,
    ModelJoinJ5Request,
    ModelJoinJ5Result,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers.handler_work_ledger_append_effect import (
    _request_lines,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    ProtocolLedgerReader,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols.join_j5 import (
    ProjectionReadFailedError,
    ProtocolJoinAppend,
    ProtocolProjectedRows,
    ProtocolStoreIdentities,
    RunDeadlineExceededError,
    StoreIdentityUnresolvedError,
    durable_row,
)

_NODE = "node_work_ledger_append_effect"
_SECTION = "join_j5"
_DURABLE = frozenset(
    {
        EnumWorkLedgerAppendStatus.ACCEPTED,
        EnumWorkLedgerAppendStatus.DUPLICATE,
    }
)


def load_join_j5_policy(contract: Mapping[str, Any] | None = None) -> ModelJoinJ5Policy:
    """The contract's ``join_j5`` section; an absent section or key refuses naming it."""
    if contract is None:
        contract = yaml.safe_load(
            resources.files(f"omnimarket.nodes.{_NODE}")
            .joinpath("contract.yaml")
            .read_text()
        )
    section = contract.get(_SECTION) if contract else None
    if not isinstance(section, dict):
        raise ValueError(f"{_NODE} contract declares no {_SECTION} section")
    for key in ModelJoinJ5Policy.model_fields:
        if key not in section:
            raise ValueError(f"{_NODE} contract {_SECTION}.{key} is absent")
    return ModelJoinJ5Policy(**section)


class HandlerWorkLedgerJoinJ5:
    def __init__(
        self,
        *,
        append: ProtocolJoinAppend | None = None,
        ledger: ProtocolLedgerReader | None = None,
        identities: ProtocolStoreIdentities | None = None,
        projection: ProtocolProjectedRows | None = None,
        sink: ProtocolEmissionSink | None = None,
        policy: ModelJoinJ5Policy | None = None,
        clock: Callable[[], datetime] | None = None,
        backend: Literal["in_memory", "real"] = "real",
    ) -> None:
        self._append = append
        self._ledger = ledger
        self._identities = identities
        self._projection = projection
        self._sink = sink
        self._policy = policy or load_join_j5_policy()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._backend = backend

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    def _wired(
        self,
    ) -> tuple[
        ProtocolJoinAppend,
        ProtocolLedgerReader,
        ProtocolStoreIdentities,
        ProtocolProjectedRows,
        ProtocolEmissionSink,
    ]:
        # Every collaborator defaults to None so the boot resolver can construct the handler from
        # injectable params alone; a handler built without one refuses at the call that needs it.
        if (
            self._append is None
            or self._ledger is None
            or self._identities is None
            or self._projection is None
            or self._sink is None
        ):
            raise RuntimeError(
                "HandlerWorkLedgerJoinJ5 is not wired: append, ledger, identities, projection and sink are required"
            )
        return (
            self._append,
            self._ledger,
            self._identities,
            self._projection,
            self._sink,
        )

    def handle(self, request: ModelJoinJ5Request) -> ModelJoinJ5Result:
        policy = self._policy
        append, ledger, identities, projection, sink = self._wired()
        chain = ChainEmitter(
            sink,
            node=policy.emission_node,
            piece=policy.to_piece,
            origin="probe",
            clock=self._clock,
            correlation_id=request.correlation_id,
            backend=self._backend,
        )
        try:
            principal = identities.resolve(request.identity_ref)
        except StoreIdentityUnresolvedError as exc:
            code = policy.identity_unresolved_failure_code
            chain.refuse(code, str(exc))
            return ModelJoinJ5Result(
                outcome=EnumJoinJ5Outcome.REFUSED_IDENTITY_UNRESOLVED,
                correlation_id=request.correlation_id,
                failure_code=code,
                detail=str(exc),
            )

        # A replay of a request already on the ledger belongs to the chain its first run emitted: a second start and
        # terminal under the same correlation id would read as duplicate_start / success_and_failure on the floor.
        replay = bool(
            _request_lines(ledger.read_text(), f"req={request.append.request_id}")
        )
        if not replay:
            chain.start()
        try:
            return self._after_start(
                request, chain, replay, principal, append, ledger, projection
            )
        except BaseException as exc:
            # Every chain this handler starts ends: a raise after the start (an unreadable file, a source that blew up, the
            # run's deadline) closes it with a typed failure, so the floor never reads start_without_terminal. A replay
            # belongs to the first run's chain and writes none. The cancel goes on after the chain is closed.
            deadline = isinstance(exc, RunDeadlineExceededError)
            code = (
                policy.deadline_exceeded_failure_code
                if deadline
                else policy.interrupted_failure_code
            )
            detail = f"{type(exc).__name__}: the run ended before its chain did" + (
                " (run deadline)" if deadline else ""
            )
            if chain.open and not replay:
                chain.failure(code, detail)
            if not isinstance(exc, Exception):
                raise
            return ModelJoinJ5Result(
                outcome=(
                    EnumJoinJ5Outcome.DEADLINE_EXCEEDED
                    if deadline
                    else EnumJoinJ5Outcome.INTERRUPTED
                ),
                correlation_id=request.correlation_id,
                failure_code=code,
                detail=detail,
            )

    def _after_start(
        self,
        request: ModelJoinJ5Request,
        chain: ChainEmitter,
        replay: bool,
        principal: str,
        append: ProtocolJoinAppend,
        ledger: ProtocolLedgerReader,
        projection: ProtocolProjectedRows,
    ) -> ModelJoinJ5Result:
        policy = self._policy
        receipt = append.handle(request.append, writer_principal=principal)
        if receipt.status not in _DURABLE or not receipt.ledger_lines:
            code = policy.append_not_accepted_failure_code
            if not replay:
                chain.failure(code, f"{receipt.status.value}: {receipt.message}")
            return ModelJoinJ5Result(
                outcome=EnumJoinJ5Outcome.APPEND_NOT_ACCEPTED,
                correlation_id=request.correlation_id,
                failure_code=code,
                detail=receipt.message,
                receipt=receipt,
            )

        # Every landed row is read back, not only the first: the request may carry several.
        ledger_seq = receipt.ledger_lines[0]
        text = ledger.read_text()
        first_projected_id: str | None = None
        for seq in receipt.ledger_lines:
            durable = durable_row(text, seq)
            # The receipt's position counts the ledger host's file. A copy that is short, stale or another file holds some
            # other row there, and a projection that holds that row would read as a landing: the durable row must be this
            # request's.
            if f"req={request.append.request_id}" not in durable:
                code = policy.append_not_accepted_failure_code
                detail = f"the ledger line at position {seq} is not the row of request {request.append.request_id}"
                if not replay:
                    chain.failure(code, detail)
                return ModelJoinJ5Result(
                    outcome=EnumJoinJ5Outcome.APPEND_NOT_ACCEPTED,
                    correlation_id=request.correlation_id,
                    failure_code=code,
                    detail=detail,
                    receipt=receipt,
                    ledger_seq=ledger_seq,
                )
            try:
                projected = projection.read_row(
                    ledger_id=request.append.ledger_id,
                    ledger_seq=seq,
                    raw_row=durable,
                    correlation_id=request.correlation_id,
                )
            except ProjectionReadFailedError as exc:
                code = policy.projection_read_failed_failure_code
                detail = str(exc)
                if not replay:
                    chain.failure(code, detail)
                return ModelJoinJ5Result(
                    outcome=EnumJoinJ5Outcome.PROJECTION_READ_FAILED,
                    correlation_id=request.correlation_id,
                    failure_code=code,
                    detail=detail,
                    receipt=receipt,
                    ledger_seq=ledger_seq,
                )
            if projected is None or projected.raw_row != durable:
                code = policy.projected_row_missing_failure_code
                detail = (
                    f"no projected row at ledger_seq {seq}"
                    if projected is None
                    else f"the projected row at ledger_seq {seq} differs from the durable row"
                )
                if not replay:
                    chain.failure(code, detail)
                return ModelJoinJ5Result(
                    outcome=EnumJoinJ5Outcome.PROJECTED_ROW_MISSING,
                    correlation_id=request.correlation_id,
                    failure_code=code,
                    detail=detail,
                    receipt=receipt,
                    ledger_seq=ledger_seq,
                )
            if first_projected_id is None:
                first_projected_id = projected.row_id

        if not replay:
            chain.success(
                {
                    "rows_appended": len(receipt.ledger_lines),
                    "rows_projected": len(receipt.ledger_lines),
                }
            )
        return ModelJoinJ5Result(
            outcome=EnumJoinJ5Outcome.LANDED,
            correlation_id=request.correlation_id,
            receipt=receipt,
            ledger_seq=ledger_seq,
            projected_row_id=first_projected_id,
        )


__all__ = ["HandlerWorkLedgerJoinJ5", "load_join_j5_policy"]
