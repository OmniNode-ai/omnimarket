# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The existing ``ProtocolLiteralWorkflowStateStore`` of omnibase_spi, over the run engine.

A run's state is owned by the engine and read from it: ``load_workflow_instance`` and ``query_workflow_instances``
answer from the engine's record. The write side of the protocol (save, delete, lock, unlock) has a meaning only for a
store the caller owns. The in-memory backend keeps those in a dictionary; a durable backend owns its own history, so
those four refuse with ``RunEngineUnsupportedError`` naming the operation, which the conformance suite pins.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from omnimarket.nodes.node_run_engine_effect.backends.protocol_run_engine_backend import (
    ProtocolRunEngineBackend,
    RunEngineUnsupportedError,
)
from omnimarket.nodes.node_run_engine_effect.handlers.adapter_spi_orchestrator import (
    WORKFLOW_TYPE,
    instance_id_for,
)
from omnimarket.nodes.node_run_engine_effect.models.model_run_engine import (
    EnumRunStatus,
)

_STATE_FOR_STATUS = {
    EnumRunStatus.RUNNING: "running",
    EnumRunStatus.WAITING: "paused",
    EnumRunStatus.COMPLETED: "completed",
    EnumRunStatus.FAILED: "failed",
    EnumRunStatus.CANCELLED: "cancelled",
}


@dataclass(frozen=True)
class SpiSnapshot:
    """Structurally a ``ProtocolWorkflowSnapshot``."""

    workflow_type: str
    instance_id: UUID
    sequence_number: int
    state: str
    context: dict[str, Any]
    tasks: list[Any]
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)

    async def validate_snapshot(self) -> bool:
        return self.sequence_number >= 0

    def is_consistent(self) -> bool:
        return bool(self.workflow_type)


class AdapterSpiStateStore:
    """``ProtocolLiteralWorkflowStateStore`` over a backend and the run keys the caller registered."""

    def __init__(self, backend: ProtocolRunEngineBackend) -> None:
        self._backend = backend
        self._known: dict[UUID, str] = {}
        self._saved: dict[tuple[str, UUID], SpiSnapshot] = {}
        self._locks: dict[tuple[str, UUID], str] = {}

    def register_run_key(self, run_key: str) -> UUID:
        """Tell the store a run key so its instance id resolves; the id is a function of the key."""
        instance_id = instance_id_for(run_key)
        self._known[instance_id] = run_key
        return instance_id

    async def _snapshot(self, run_key: str) -> SpiSnapshot | None:
        state = await self._backend.get_run_state(run_key)
        if state.status is EnumRunStatus.NOT_FOUND:
            return None
        return SpiSnapshot(
            workflow_type=WORKFLOW_TYPE,
            instance_id=instance_id_for(run_key),
            sequence_number=len(state.recorded),
            state=_STATE_FOR_STATUS[state.status],
            context={"waiting_on": state.waiting_on or ""},
            tasks=[],
            created_at=datetime.now(UTC),
            metadata={"backend": self._backend.name},
        )

    async def load_workflow_instance(
        self, workflow_type: str, instance_id: UUID
    ) -> SpiSnapshot | None:
        if workflow_type != WORKFLOW_TYPE:
            return None
        run_key = self._known.get(instance_id)
        if run_key is None:
            return self._saved.get((workflow_type, instance_id))
        return await self._snapshot(run_key)

    async def query_workflow_instances(
        self,
        workflow_type: str | None,
        state: str | None,
        correlation_id: UUID | None,
        limit: int,
        offset: int,
    ) -> list[SpiSnapshot]:
        if workflow_type not in (None, WORKFLOW_TYPE):
            return []
        found: list[SpiSnapshot] = []
        for run_key in sorted(self._known.values()):
            snapshot = await self._snapshot(run_key)
            if snapshot is not None and state in (None, snapshot.state):
                found.append(snapshot)
        return found[offset : offset + limit]

    def _require_owned_store(self, operation: str) -> None:
        if self._backend.durable:
            raise RunEngineUnsupportedError(
                f"{operation}: the {self._backend.name} backend owns its run history"
            )

    async def save_workflow_instance(self, workflow_instance: Any) -> bool:
        self._require_owned_store("save_workflow_instance")
        self._saved[
            (workflow_instance.workflow_type, workflow_instance.instance_id)
        ] = workflow_instance
        return True

    async def delete_workflow_instance(
        self, workflow_type: str, instance_id: UUID
    ) -> bool:
        self._require_owned_store("delete_workflow_instance")
        return self._saved.pop((workflow_type, instance_id), None) is not None

    async def lock_workflow_instance(
        self,
        workflow_type: str,
        instance_id: UUID,
        lock_owner: str,
        timeout_seconds: int,
    ) -> bool:
        self._require_owned_store("lock_workflow_instance")
        key = (workflow_type, instance_id)
        if self._locks.get(key, lock_owner) != lock_owner:
            return False
        self._locks[key] = lock_owner
        return True

    async def unlock_workflow_instance(
        self, workflow_type: str, instance_id: UUID, lock_owner: str
    ) -> bool:
        self._require_owned_store("unlock_workflow_instance")
        key = (workflow_type, instance_id)
        if self._locks.get(key) != lock_owner:
            return False
        del self._locks[key]
        return True
