# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the five handlers of node_dev_roll_gate_effect resolve the same way (OMN-24431).

The runtime builds a handler with no arguments; the policy and the store are then
resolved lazily on the first request (constructor arguments, else the overlay) and
cached. The handlers of one node share one store: in a deployment each resolves the
same file store, which every ``save`` rewrites whole, so the runtime must run them
serially (one consumer per node, as the existing dev roll nodes do).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from omnimarket.nodes.node_dev_roll_gate_effect.handlers.store_dev_roll_gate import (
    store_from_policy,
)
from omnimarket.nodes.node_dev_roll_gate_effect.models import (
    EnumDevRollGateObservedReason,
    ModelDevRollGateObserved,
    ModelDevRollGatePolicy,
)
from omnimarket.nodes.node_dev_roll_gate_effect.overlay import load_gate_policy
from omnimarket.nodes.node_dev_roll_gate_effect.protocols import (
    ProtocolDevRollGateStore,
)


class HandlerDevRollGateBase:
    """Policy, store and clock of one gate handler."""

    def __init__(
        self,
        store: ProtocolDevRollGateStore | None = None,
        *,
        policy: ModelDevRollGatePolicy | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._policy = policy
        self._environ = environ
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def _resolve_policy(self) -> ModelDevRollGatePolicy:
        if self._policy is None:
            environ = os.environ if self._environ is None else self._environ
            self._policy = load_gate_policy(environ)
        return self._policy

    def _resolve_store(self) -> ProtocolDevRollGateStore:
        if self._store is None:
            environ = os.environ if self._environ is None else self._environ
            self._store = store_from_policy(self._resolve_policy(), environ)
        return self._store

    @staticmethod
    def _observed(
        reason: EnumDevRollGateObservedReason,
        correlation_id: str | None = None,
        detail: str | None = None,
    ) -> ModelDevRollGateObserved:
        return ModelDevRollGateObserved(
            reason=reason, correlation_id=correlation_id, detail=detail
        )
