"""Declarative effect node for the exception queue (OMN-17427)."""

from __future__ import annotations

from pathlib import Path

from .models import (
    ModelExceptionEmitRequest,
    ModelExceptionEmitResult,
    ModelExceptionQueueRead,
    ModelExceptionQueueReadRequest,
    ModelExceptionQueueRequest,
    ModelExceptionQueueResult,
    ModelExceptionResolveRequest,
    ModelExceptionResolveResult,
)

CONTRACT = Path(__file__).with_name("contract.yaml")


class NodeExceptionQueueEffect:
    contract_path = CONTRACT

    def handle(
        self,
        request: ModelExceptionEmitRequest
        | ModelExceptionQueueRequest
        | ModelExceptionResolveRequest
        | ModelExceptionQueueReadRequest,
    ) -> (
        ModelExceptionEmitResult
        | ModelExceptionQueueResult
        | ModelExceptionResolveResult
        | ModelExceptionQueueRead
    ):
        from .handler import (
            HandlerExceptionEmit,
            HandlerExceptionQueue,
            HandlerExceptionQueueRead,
            HandlerExceptionResolve,
        )

        if isinstance(request, ModelExceptionEmitRequest):
            return HandlerExceptionEmit().handle(request)
        if isinstance(request, ModelExceptionQueueRequest):
            return HandlerExceptionQueue().handle(request)
        if isinstance(request, ModelExceptionQueueReadRequest):
            return HandlerExceptionQueueRead().handle(request)
        if isinstance(request, ModelExceptionResolveRequest):
            return HandlerExceptionResolve().handle(request)
        raise TypeError(
            f"Unsupported exception queue request: {type(request).__name__}"
        )
