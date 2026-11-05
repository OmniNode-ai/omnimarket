# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Route one run engine operation to the backend the overlay selected.

A backend that cannot be reached or answers no usable answer returns ``unknown`` for a start, release or cancel and an
unhealthy health read, never an exception and never an assumed success: the caller treats ``unknown`` as not done.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.nodes.node_run_engine_effect.backends.protocol_run_engine_backend import (
    ProtocolRunEngineBackend,
    RunEngineBackendUnavailableError,
)
from omnimarket.nodes.node_run_engine_effect.handlers.overlay_run_engine import (
    load_run_engine_overlay,
    shared_backend,
)
from omnimarket.nodes.node_run_engine_effect.models.model_run_engine import (
    EnumCancelOutcome,
    EnumReleaseOutcome,
    EnumRunEngineOperation,
    EnumStartOutcome,
    ModelRunEngineRequest,
    ModelRunEngineResult,
)


class HandlerRunEngine:
    """The node's handler: an injected backend, or the process's one backend for the overlay, one operation per call."""

    def __init__(
        self,
        backend: ProtocolRunEngineBackend | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._backend = backend or shared_backend(load_run_engine_overlay(environ))

    @property
    def backend(self) -> ProtocolRunEngineBackend:
        return self._backend

    async def handle(self, request: ModelRunEngineRequest) -> ModelRunEngineResult:
        op = request.operation
        name = self._backend.name
        try:
            if op is EnumRunEngineOperation.START_RUN and request.run is not None:
                return ModelRunEngineResult(
                    operation=op,
                    backend=name,
                    start_outcome=await self._backend.start_run(request.run),
                )
            if (
                op is EnumRunEngineOperation.RELEASE_RUN
                and request.run_key is not None
                and request.condition is not None
                and request.release_id is not None
            ):
                return ModelRunEngineResult(
                    operation=op,
                    backend=name,
                    release_outcome=await self._backend.release_run(
                        request.run_key, request.condition, request.release_id
                    ),
                )
            if op is EnumRunEngineOperation.CANCEL_RUN and request.run_key is not None:
                return ModelRunEngineResult(
                    operation=op,
                    backend=name,
                    cancel_outcome=await self._backend.cancel_run(request.run_key),
                )
            if (
                op is EnumRunEngineOperation.GET_RUN_STATE
                and request.run_key is not None
            ):
                return ModelRunEngineResult(
                    operation=op,
                    backend=name,
                    state=await self._backend.get_run_state(request.run_key),
                )
            if op is EnumRunEngineOperation.HEALTH:
                return ModelRunEngineResult(
                    operation=op, backend=name, healthy=await self._backend.health()
                )
        except RunEngineBackendUnavailableError as unavailable:
            return self._unknown(op, str(unavailable))
        raise ValueError(
            f"{op.value}: the request lacks the fields the operation needs"
        )

    def _unknown(self, op: EnumRunEngineOperation, detail: str) -> ModelRunEngineResult:
        name = self._backend.name
        if op is EnumRunEngineOperation.START_RUN:
            return ModelRunEngineResult(
                operation=op,
                backend=name,
                detail=detail,
                start_outcome=EnumStartOutcome.UNKNOWN,
            )
        if op is EnumRunEngineOperation.RELEASE_RUN:
            return ModelRunEngineResult(
                operation=op,
                backend=name,
                detail=detail,
                release_outcome=EnumReleaseOutcome.UNKNOWN,
            )
        if op is EnumRunEngineOperation.CANCEL_RUN:
            return ModelRunEngineResult(
                operation=op,
                backend=name,
                detail=detail,
                cancel_outcome=EnumCancelOutcome.UNKNOWN,
            )
        if op is EnumRunEngineOperation.HEALTH:
            return ModelRunEngineResult(
                operation=op, backend=name, detail=detail, healthy=False
            )
        return ModelRunEngineResult(operation=op, backend=name, detail=detail)
