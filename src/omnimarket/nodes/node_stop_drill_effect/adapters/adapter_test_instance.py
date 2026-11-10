# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The test admission instance of the stop drill: the gate store's in-memory twin behind the J6 join.

The instance is built per run from the overlay's limits and the join's contract policy; nothing about it is spelled
here. ``AcceptingGateStore`` is the fault the plan injects on purpose: a store that admits every reservation, stops
included, so a send above a stop is accepted.
"""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.model_gate_store import (
    EnumGateStoreBackend,
    EnumGateStoreOperation,
    EnumGateStoreOutcome,
    ModelGateStorePolicy,
    ModelGateStoreRequest,
    ModelGateStoreResult,
)
from omnimarket.nodes.node_gate_store_effect.handlers.adapters_join_j6 import (
    InMemoryJoinJ6AuditSink,
    InMemoryJoinJ6Projection,
)
from omnimarket.nodes.node_gate_store_effect.handlers.handler_gate_store_join_j6 import (
    HandlerGateStoreJoinJ6,
    load_join_j6_policy,
)
from omnimarket.nodes.node_gate_store_effect.handlers.store_in_memory import (
    InMemoryGateStore,
)
from omnimarket.nodes.node_stop_drill_effect.models import ModelStopDrillOverlay
from omnimarket.protocols.protocol_gate_store import ProtocolGateStore


class AcceptingGateStore:
    """The injected fault: a store that admits every reservation whatever the stop state says."""

    def __init__(self, inner: ProtocolGateStore) -> None:
        self._inner = inner

    def transact(self, request: ModelGateStoreRequest) -> ModelGateStoreResult:
        if request.operation is EnumGateStoreOperation.RESERVE:
            return ModelGateStoreResult(
                correlation_id=request.correlation_id,
                operation=request.operation,
                outcome=EnumGateStoreOutcome.OK,
                epoch=0,
                detail="injected fault: the instance accepts the lane above a stop",
            )
        return self._inner.transact(request)


class ProtocolDrillInstanceFactory(Protocol):
    """Builds one test admission instance; ``accepting`` plants the fault."""

    def __call__(
        self, overlay: ModelStopDrillOverlay, /, *, accepting: bool
    ) -> HandlerGateStoreJoinJ6: ...


def build_test_instance(
    overlay: ModelStopDrillOverlay, *, accepting: bool
) -> HandlerGateStoreJoinJ6:
    twin = InMemoryGateStore(
        ModelGateStorePolicy(
            backend=EnumGateStoreBackend.IN_MEMORY,
            limits=dict(overlay.store_limits),
            reservation_ttl_seconds=overlay.reservation_ttl_seconds,
            order=(),
        )
    )
    store: ProtocolGateStore = AcceptingGateStore(twin) if accepting else twin
    return HandlerGateStoreJoinJ6(
        store,
        InMemoryJoinJ6AuditSink(),
        InMemoryJoinJ6Projection(),
        load_join_j6_policy(),
    )
