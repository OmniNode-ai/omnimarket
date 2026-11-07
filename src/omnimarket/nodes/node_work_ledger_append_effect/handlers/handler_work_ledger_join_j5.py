# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J5 (piece 5 to piece 6): an append under a store-resolved identity lands as a durable row and a projected row (OMN-22914).

Canonical shape: ``handle(ModelJoinJ5Request) -> ModelJoinJ5Result``. The handler answers piece 5 on the floor's
emission contract, under the correlation id piece 5's chain carries:

* the store gives no identity: a typed ``upstream_`` failure that never started, and no row anywhere;
* the identity resolves: a start, the append under that principal, then the row read back from the projection at the
  receipt's position; a success only when both the durable row and the projected row are there, and a typed failure
  with the start before it when the append is not accepted or the projection never heard the row.

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
    HandlerWorkLedgerAppendEffect,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols import (
    ProtocolLedgerReader,
)
from omnimarket.nodes.node_work_ledger_append_effect.protocols.join_j5 import (
    ProtocolProjectedRows,
    ProtocolStoreIdentities,
    StoreIdentityUnresolvedError,
)

_NODE = "node_work_ledger_append_effect"
_SECTION = "join_j5"
_DURABLE = frozenset(
    {
        EnumWorkLedgerAppendStatus.ACCEPTED,
        EnumWorkLedgerAppendStatus.DUPLICATE,
        EnumWorkLedgerAppendStatus.ALREADY_PRESENT,
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
        append: HandlerWorkLedgerAppendEffect,
        ledger: ProtocolLedgerReader,
        identities: ProtocolStoreIdentities,
        projection: ProtocolProjectedRows,
        sink: ProtocolEmissionSink,
        policy: ModelJoinJ5Policy,
        clock: Callable[[], datetime] | None = None,
        backend: Literal["in_memory", "real"] = "real",
    ) -> None:
        self._append = append
        self._ledger = ledger
        self._identities = identities
        self._projection = projection
        self._sink = sink
        self._policy = policy
        self._clock = clock or (lambda: datetime.now(UTC))
        self._backend = backend

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["EFFECT"]:
        return "EFFECT"

    def handle(self, request: ModelJoinJ5Request) -> ModelJoinJ5Result:
        policy = self._policy
        chain = ChainEmitter(
            self._sink,
            node=policy.emission_node,
            piece=policy.to_piece,
            clock=self._clock,
            correlation_id=request.correlation_id,
            backend=self._backend,
        )
        try:
            principal = self._identities.resolve(request.identity_ref)
        except StoreIdentityUnresolvedError as exc:
            code = policy.identity_unresolved_failure_code
            chain.refuse(code, str(exc))
            return ModelJoinJ5Result(
                outcome=EnumJoinJ5Outcome.REFUSED_IDENTITY_UNRESOLVED,
                correlation_id=request.correlation_id,
                failure_code=code,
                detail=str(exc),
            )

        chain.start()
        receipt = self._append.handle(request.append, writer_principal=principal)
        if receipt.status not in _DURABLE or not receipt.ledger_lines:
            code = policy.append_not_accepted_failure_code
            chain.failure(code, f"{receipt.status.value}: {receipt.message}")
            return ModelJoinJ5Result(
                outcome=EnumJoinJ5Outcome.APPEND_NOT_ACCEPTED,
                correlation_id=request.correlation_id,
                failure_code=code,
                detail=receipt.message,
                receipt=receipt,
            )

        ledger_seq = receipt.ledger_lines[0]
        durable = self._ledger.read_text().splitlines()[ledger_seq - 1]
        projected = self._projection.read_row(
            ledger_id=request.append.ledger_id, ledger_seq=ledger_seq
        )
        if projected is None or projected.raw_row != durable:
            code = policy.projected_row_missing_failure_code
            detail = (
                f"no projected row at ledger_seq {ledger_seq}"
                if projected is None
                else f"the projected row at ledger_seq {ledger_seq} differs from the durable row"
            )
            chain.failure(code, detail)
            return ModelJoinJ5Result(
                outcome=EnumJoinJ5Outcome.PROJECTED_ROW_MISSING,
                correlation_id=request.correlation_id,
                failure_code=code,
                detail=detail,
                receipt=receipt,
                ledger_seq=ledger_seq,
            )

        chain.success({"rows_appended": 1, "rows_projected": 1})
        return ModelJoinJ5Result(
            outcome=EnumJoinJ5Outcome.LANDED,
            correlation_id=request.correlation_id,
            receipt=receipt,
            ledger_seq=ledger_seq,
            projected_row_id=projected.row_id,
        )


__all__ = ["HandlerWorkLedgerJoinJ5", "load_join_j5_policy"]
