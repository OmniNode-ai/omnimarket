# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lazily resolved policy, store and clock the three gate handlers share."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

from omnimarket.nodes.node_dev_roll_window_gate_effect.handlers.store_dev_roll_gate import (
    store_from_policy,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    ModelDevRollWindowGatePolicy,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.overlay import load_gate_policy
from omnimarket.nodes.node_dev_roll_window_gate_effect.protocols import (
    ProtocolDevRollGateStore,
)


class DevRollGateContext:
    """The runtime builds each handler with no arguments; the overlay then supplies policy and store.

    A test passes a policy, a store and a fixed clock in.
    """

    def __init__(
        self,
        store: ProtocolDevRollGateStore | None = None,
        *,
        policy: ModelDevRollWindowGatePolicy | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._policy = policy
        self._environ = environ
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def now(self) -> datetime:
        return self._clock()

    def policy(self) -> ModelDevRollWindowGatePolicy:
        if self._policy is None:
            environ = os.environ if self._environ is None else self._environ
            self._policy = load_gate_policy(environ)
        return self._policy

    def store(self) -> ProtocolDevRollGateStore:
        if self._store is None:
            environ = os.environ if self._environ is None else self._environ
            self._store = store_from_policy(self.policy(), environ)
        return self._store
