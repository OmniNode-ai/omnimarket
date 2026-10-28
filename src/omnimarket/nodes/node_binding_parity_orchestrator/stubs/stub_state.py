# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""State store reference bindings and mutations of individual persistence rules."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from omnibase_core.models.state.model_state_envelope import ModelStateEnvelope
from omnibase_core.protocols.storage.protocol_state_store import ProtocolStateStore
from omnibase_core.services.state.service_state_disk import ServiceStateDisk


@dataclass
class StateHarness:
    """A store and the binding another process would see after a restart."""

    store: ProtocolStateStore
    restart: Callable[[], Awaitable[ProtocolStateStore]]


class StubStateMemory:
    """A reference dictionary store whose data survives simulated process restarts."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], ModelStateEnvelope] = {}

    async def get(
        self, node_id: str, scope_id: str = "default"
    ) -> ModelStateEnvelope | None:
        envelope = self._data.get((node_id, scope_id))
        return envelope.model_copy(deep=True) if envelope is not None else None

    async def put(self, envelope: ModelStateEnvelope) -> None:
        self._data[(envelope.node_id, envelope.scope_id)] = envelope.model_copy(
            deep=True
        )

    async def delete(self, node_id: str, scope_id: str = "default") -> bool:
        return self._data.pop((node_id, scope_id), None) is not None

    async def exists(self, node_id: str, scope_id: str = "default") -> bool:
        return (node_id, scope_id) in self._data

    async def list_keys(self, node_id: str | None = None) -> list[tuple[str, str]]:
        return [key for key in self._data if node_id is None or key[0] == node_id]

    async def restart(self) -> ProtocolStateStore:
        restarted = StubStateMemory()
        restarted._data = {
            key: value.model_copy(deep=True) for key, value in self._data.items()
        }
        return restarted


def build_memory_state_harness() -> StateHarness:
    """Build a fresh reference dictionary binding."""
    store = StubStateMemory()
    return StateHarness(store, store.restart)


def build_disk_state_harness(root: Path) -> StateHarness:
    """Reopen the same persisted data when simulating a process restart."""

    async def restart() -> ProtocolStateStore:
        return ServiceStateDisk(state_root=root)

    return StateHarness(ServiceStateDisk(state_root=root), restart)


class _LosesLastWrite(StubStateMemory):
    def __init__(self) -> None:
        super().__init__()
        self._last: tuple[str, str] | None = None
        self._previous: ModelStateEnvelope | None = None

    async def put(self, envelope: ModelStateEnvelope) -> None:
        self._last = (envelope.node_id, envelope.scope_id)
        self._previous = await super().get(*self._last)
        await super().put(envelope)

    async def restart(self) -> ProtocolStateStore:
        restarted = await super().restart()
        if self._last is not None:
            if self._previous is None:
                await restarted.delete(*self._last)
            else:
                await restarted.put(self._previous)
        return restarted


class _StaleRead(StubStateMemory):
    def __init__(self) -> None:
        super().__init__()
        self._previous: dict[tuple[str, str], ModelStateEnvelope] = {}

    async def put(self, envelope: ModelStateEnvelope) -> None:
        key = (envelope.node_id, envelope.scope_id)
        previous = await super().get(*key)
        if previous is not None:
            self._previous[key] = previous
        await super().put(envelope)

    async def get(
        self, node_id: str, scope_id: str = "default"
    ) -> ModelStateEnvelope | None:
        return self._previous.get((node_id, scope_id)) or await super().get(
            node_id, scope_id
        )


class _ExistsAlwaysTrue(StubStateMemory):
    async def exists(self, node_id: str, scope_id: str = "default") -> bool:
        return True


class _IgnoresNodeFilter(StubStateMemory):
    async def list_keys(self, node_id: str | None = None) -> list[tuple[str, str]]:
        return await super().list_keys()


class _TextOrderedKeys(StubStateMemory):
    async def list_keys(self, node_id: str | None = None) -> list[tuple[str, str]]:
        return sorted(await super().list_keys(node_id))


def build_loses_last_write_on_restart_harness() -> StateHarness:
    """Discard the latest write when reopening the store."""
    store = _LosesLastWrite()
    return StateHarness(store, store.restart)


def build_stale_read_harness() -> StateHarness:
    """Return an unlabelled prior value after an overwrite."""
    store = _StaleRead()
    return StateHarness(store, store.restart)


def build_exists_always_true_harness() -> StateHarness:
    """Claim every key exists, including missing and deleted keys."""
    store = _ExistsAlwaysTrue()
    return StateHarness(store, store.restart)


def build_list_keys_ignores_node_filter_harness() -> StateHarness:
    """Return every node's keys even when a node filter was supplied."""
    store = _IgnoresNodeFilter()
    return StateHarness(store, store.restart)


def build_text_ordered_keys_harness() -> StateHarness:
    """Return keys in text order, which the protocol permits."""
    store = _TextOrderedKeys()
    return StateHarness(store, store.restart)
