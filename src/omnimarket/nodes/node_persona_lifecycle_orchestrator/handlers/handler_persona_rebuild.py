# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Persona lifecycle rebuild handler.

The contract routes both runtime ticks and on-demand commands to this handler.
It intentionally performs orchestration only: candidate discovery and persona
rebuild execution are injected runtime ports owned by the memory runtime.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable, Sequence
from typing import Protocol
from uuid import UUID

from omnimarket.nodes.node_persona_lifecycle_orchestrator.models import (
    ModelPersonaLifecycleRequest,
    ModelPersonaLifecycleResponse,
)

__all__ = [
    "HandlerPersonaRebuild",
    "ProtocolPersonaRebuildCandidateProvider",
    "ProtocolPersonaRebuildPort",
]

_DEFAULT_BATCH_SIZE = 100


class ProtocolPersonaRebuildCandidateProvider(Protocol):
    """Runtime port for selecting users whose persona snapshots should rebuild."""

    async def list_persona_rebuild_candidates(self, limit: int) -> Sequence[str]:
        """Return user IDs eligible for persona rebuild, capped by ``limit``."""
        ...


class ProtocolPersonaRebuildPort(Protocol):
    """Runtime port that rebuilds a persona snapshot for one user."""

    async def rebuild_persona(
        self,
        user_id: str,
        correlation_id: UUID | None = None,
    ) -> bool:
        """Return True when a persona snapshot was created, False when skipped."""
        ...


class HandlerPersonaRebuild:
    """Coordinate tick-driven and on-demand persona snapshot rebuilds."""

    handler_type = "NODE_HANDLER"
    handler_category = "ORCHESTRATOR"

    def __init__(
        self,
        candidate_provider: ProtocolPersonaRebuildCandidateProvider | None = None,
        rebuild_port: ProtocolPersonaRebuildPort | None = None,
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> None:
        self._candidate_provider = candidate_provider
        self._rebuild_port = rebuild_port
        self._batch_size = _bounded_batch_size(batch_size)

    async def initialize(
        self,
        candidate_provider: ProtocolPersonaRebuildCandidateProvider | None = None,
        rebuild_port: ProtocolPersonaRebuildPort | None = None,
        batch_size: int | None = None,
    ) -> None:
        """Inject runtime ports after construction."""
        if candidate_provider is not None:
            self._candidate_provider = candidate_provider
        if rebuild_port is not None:
            self._rebuild_port = rebuild_port
        if batch_size is not None:
            self._batch_size = _bounded_batch_size(batch_size)

    async def handle(
        self,
        correlation_id: UUID | None,
        input_data: ModelPersonaLifecycleRequest,
    ) -> ModelPersonaLifecycleResponse:
        """Dispatch to the contract-routed operation for generic runtimes."""
        if input_data.operation == "on_tick":
            return await self.on_tick(correlation_id, input_data)
        if input_data.operation == "on_demand":
            return await self.on_demand(correlation_id, input_data)
        return _error_response(
            f"unsupported persona lifecycle operation: {input_data.operation}"
        )

    async def on_tick(
        self,
        correlation_id: UUID | None,
        input_data: ModelPersonaLifecycleRequest,
    ) -> ModelPersonaLifecycleResponse:
        """Process one runtime tick by rebuilding at most 100 candidate users."""
        if input_data.operation != "on_tick":
            return _error_response("on_tick route received non on_tick request")
        if self._candidate_provider is None:
            return _error_response(
                "persona rebuild candidate provider is not configured"
            )

        candidates = await self._candidate_provider.list_persona_rebuild_candidates(
            limit=self._batch_size
        )
        return await self._rebuild_users(candidates[: self._batch_size], correlation_id)

    async def on_demand(
        self,
        correlation_id: UUID | None,
        input_data: ModelPersonaLifecycleRequest,
    ) -> ModelPersonaLifecycleResponse:
        """Process an on-demand persona rebuild command for one user."""
        if input_data.operation != "on_demand":
            return _error_response("on_demand route received non on_demand request")
        if not input_data.user_id:
            return _error_response("on_demand persona rebuild requires user_id")

        return await self._rebuild_users([input_data.user_id], correlation_id)

    async def _rebuild_users(
        self,
        user_ids: Iterable[str],
        correlation_id: UUID | None,
    ) -> ModelPersonaLifecycleResponse:
        if self._rebuild_port is None:
            return _error_response("persona rebuild port is not configured")

        users_processed = 0
        personas_created = 0
        users_skipped = 0

        for user_id in _unique_non_empty_user_ids(user_ids):
            users_processed += 1
            try:
                created = await _call_rebuild_port(
                    self._rebuild_port,
                    user_id=user_id,
                    correlation_id=correlation_id,
                )
            except Exception as exc:
                return _error_response(
                    f"persona rebuild failed for user_id={user_id}: {exc}"
                )

            if created:
                personas_created += 1
            else:
                users_skipped += 1

        return ModelPersonaLifecycleResponse(
            status="success",
            users_processed=users_processed,
            personas_created=personas_created,
            users_skipped=users_skipped,
        )


def _bounded_batch_size(batch_size: int) -> int:
    if batch_size < 1:
        return 1
    return min(batch_size, _DEFAULT_BATCH_SIZE)


def _unique_non_empty_user_ids(user_ids: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for user_id in user_ids:
        normalized = user_id.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return unique


async def _call_rebuild_port(
    rebuild_port: ProtocolPersonaRebuildPort,
    user_id: str,
    correlation_id: UUID | None,
) -> bool:
    method = rebuild_port.rebuild_persona
    parameters = inspect.signature(method).parameters
    if "correlation_id" in parameters:
        result = await method(user_id=user_id, correlation_id=correlation_id)
    else:
        result = await method(user_id)
    return bool(result)


def _error_response(error_message: str) -> ModelPersonaLifecycleResponse:
    return ModelPersonaLifecycleResponse(
        status="error",
        error_message=error_message,
    )
