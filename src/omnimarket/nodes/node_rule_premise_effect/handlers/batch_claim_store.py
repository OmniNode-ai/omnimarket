# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory binding for the Part A transaction protocol; deployments bind their claim store."""

from collections.abc import Iterator
from contextlib import contextmanager
from threading import RLock

from omnimarket.models.model_rule_premise import ModelBatchSlot, ModelBatchState
from omnimarket.protocols.protocol_rule_premise import ProtocolBatchTransaction


class _MemoryTransaction:
    def __init__(
        self, states: dict[str, ModelBatchState], keys: set[str], batch: str
    ) -> None:
        self.states, self.keys, self.batch = states, keys, batch

    def read(self) -> ModelBatchState | None:
        return self.states.get(self.batch)

    def insert_slot(self, slot: ModelBatchSlot) -> bool:
        if slot.key in self.keys:
            return False
        self.keys.add(slot.key)
        return True

    def write(self, state: ModelBatchState) -> None:
        self.states[self.batch] = state


class MemoryBatchClaimStore:
    def __init__(self) -> None:
        self._states: dict[str, ModelBatchState] = {}
        self._keys: set[str] = set()
        self._lock = RLock()

    @contextmanager
    def transaction(self, batch: str) -> Iterator[ProtocolBatchTransaction]:
        with self._lock:
            states, keys = dict(self._states), set(self._keys)
            yield _MemoryTransaction(states, keys, batch)
            self._states, self._keys = states, keys
