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
    ModelOpenStopsRequest,
    ModelOpenStopsResult,
    ModelStopEventRequest,
    ModelStopEventResult,
)

CONTRACT = Path(__file__).with_name("contract.yaml")


class NodeExceptionQueueEffect:
    contract_path = CONTRACT

    def handle(
        self,
        request: ModelExceptionEmitRequest
        | ModelExceptionQueueRequest
        | ModelExceptionResolveRequest
        | ModelExceptionQueueReadRequest
        | ModelStopEventRequest
        | ModelOpenStopsRequest,
    ) -> (
        ModelExceptionEmitResult
        | ModelExceptionQueueResult
        | ModelExceptionResolveResult
        | ModelExceptionQueueRead
        | ModelStopEventResult
        | ModelOpenStopsResult
    ):
        from .container.container_exception_queue_effect import (
            create_handler_exception_emit,
            create_handler_exception_queue,
            create_handler_exception_queue_read,
            create_handler_exception_resolve,
            create_handler_open_stops,
            create_handler_stop_event,
        )

        if isinstance(request, ModelExceptionEmitRequest):
            return create_handler_exception_emit().handle(request)
        if isinstance(request, ModelExceptionQueueRequest):
            return create_handler_exception_queue().handle(request)
        if isinstance(request, ModelExceptionQueueReadRequest):
            return create_handler_exception_queue_read().handle(request)
        if isinstance(request, ModelExceptionResolveRequest):
            return create_handler_exception_resolve().handle(request)
        if isinstance(request, ModelStopEventRequest):
            return create_handler_stop_event().handle(request)
        if isinstance(request, ModelOpenStopsRequest):
            return create_handler_open_stops().handle(request)
        raise TypeError(
            f"Unsupported exception queue request: {type(request).__name__}"
        )
