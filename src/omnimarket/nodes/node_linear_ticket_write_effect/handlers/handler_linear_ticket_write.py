# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Linear writes run behind this effect; callers receive typed terminals.

Like the SCM effect, replay records are bounded and in-process. A timeout is
an unknown outcome: read before retrying across a worker restart. Concurrent
redeliveries are serialized so they cannot both pass the replay check.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import OrderedDict
from collections.abc import Callable
from typing import Protocol, TypedDict
from uuid import UUID

from omnimarket.handlers.handler_lane_intake import LaneIntakeRefusedError
from omnimarket.inference.secret_store_resolver import SecretStoreConfigurationError
from omnimarket.models.model_ticket_io import (
    EnumTicketOperation,
    ModelCreateTicketRequest,
    ModelCreateTicketResult,
)
from omnimarket.models.secret_store.model_secret_resolution_error import (
    SecretResolutionError,
)
from omnimarket.models.ticket_write_io import (
    ModelTicketWriteCompleted,
    ModelTicketWriteFailed,
    ModelTicketWriteRequest,
)
from omnimarket.nodes.node_create_ticket import (
    HandlerCreateTicket,
    TicketGuardRefusedError,
    TicketGuardUnavailableError,
)

Result = ModelTicketWriteCompleted | ModelTicketWriteFailed
_RECORD_LIMIT = 1024
_LOG = logging.getLogger(__name__)


def _handler(lane: str) -> HandlerCreateTicket:
    return HandlerCreateTicket(lane_id=lane, require_description_stamp=True)


class _Identity(TypedDict):
    correlation_id: UUID
    operation: EnumTicketOperation
    idempotency_key: str
    requested_by_lane: str
    requested_by_run: str | None


class ProtocolTicketHandler(Protocol):
    def handle(self, request: ModelCreateTicketRequest) -> ModelCreateTicketResult: ...


class HandlerLinearTicketWriteEffect:
    """Run the existing guard, adapter and create gateway with explicit lane identity."""

    def __init__(
        self, *, handler_factory: Callable[[str], ProtocolTicketHandler] = _handler
    ) -> None:
        self._factory = handler_factory
        self._lock = asyncio.Lock()
        self._inflight: set[asyncio.Task[Result]] = set()
        self._done: OrderedDict[
            tuple[str, str, str, str], tuple[str, ModelTicketWriteCompleted]
        ] = OrderedDict()

    async def handle(self, request: ModelTicketWriteRequest) -> Result:
        # Canceling an await cannot stop the synchronous gateway thread. Keep
        # its lock and replay update alive until it finishes, so a retry waits
        # for that write instead of starting another one alongside it.
        task = asyncio.create_task(self._execute(request))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
        return await asyncio.shield(task)

    async def _execute(self, request: ModelTicketWriteRequest) -> Result:
        identity: _Identity = {
            "correlation_id": request.correlation_id,
            "operation": request.operation,
            "idempotency_key": request.idempotency_key,
            "requested_by_lane": request.requested_by_lane,
            "requested_by_run": request.requested_by_run,
        }

        def failed(
            reason: str, codes: list[str] | None = None
        ) -> ModelTicketWriteFailed:
            return ModelTicketWriteFailed(
                **identity,
                reason=reason,
                terminal_failure_cause=reason,
                finding_codes=codes or [],
            )

        # Correlation ids change on retry; the operation and lane/run identity do not.
        fingerprint = hashlib.sha256(
            json.dumps(
                request.model_dump(mode="json", exclude={"correlation_id"}),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        key = (
            request.team,
            request.requested_by_lane,
            request.operation.value,
            request.idempotency_key,
        )
        cacheable = (
            request.operation is not EnumTicketOperation.READ and not request.dry_run
        )
        async with self._lock:
            if cacheable and key in self._done:
                previous, cached = self._done[key]
                if previous != fingerprint:
                    return failed("idempotency_conflict")
                return cached.model_copy(
                    update={"correlation_id": request.correlation_id, "replayed": True}
                )
            try:
                core = self._factory(request.requested_by_lane)
                result = await asyncio.to_thread(core.handle, request.ticket_request())
            except TicketGuardRefusedError as exc:
                return failed("guard_refused", exc.finding_codes)
            except (TicketGuardUnavailableError, LaneIntakeRefusedError):
                return failed("guard_unavailable")
            except (SecretResolutionError, SecretStoreConfigurationError):
                # The consumer cannot resolve the Linear key: say so, with the
                # exception type only (its text is never put on a terminal).
                _LOG.warning("linear ticket write: Linear key unresolved")
                return failed("secret_unavailable", ["linear_api_key_unresolved"])
            except Exception as exc:
                # Exception text may contain HTTP headers or adapter credentials,
                # so the log and the terminal carry the type name alone.
                _LOG.warning(
                    "linear ticket write: operation failed (%s)", type(exc).__name__
                )
                return failed("operation_failed")
            if result.status == "error":
                return failed("invalid_request", result.finding_codes)
            completed = ModelTicketWriteCompleted(**identity, result=result)
            if cacheable:
                self._done[key] = (fingerprint, completed)
                while len(self._done) > _RECORD_LIMIT:
                    self._done.popitem(last=False)
            return completed
