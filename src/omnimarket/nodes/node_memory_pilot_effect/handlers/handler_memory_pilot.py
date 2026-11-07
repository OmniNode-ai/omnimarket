# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Memory pilot effect: ingest and ask through one backend, on a frozen bar, on lab models only."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import ClassVar

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.contract_topics import contract_publish_topics
from omnimarket.nodes.node_memory_pilot_effect.handlers.backend_in_memory import (
    BackendInMemory,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.frozen_pilot import (
    PilotNotFrozenError,
    frozen_system_prompt,
    verify_frozen,
)
from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot import (
    BACKEND_REQUIREMENTS,
    EnumMemoryPilotBackend,
    EnumMemoryPilotOperation,
    EnumMemoryPilotReading,
    EnumMemoryPilotStatus,
    ModelMemoryPilotOverlay,
    ModelMemoryPilotRequest,
    ModelMemoryPilotResult,
)
from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot import (
    EnumMemoryPilotErrorCode as Code,
)
from omnimarket.nodes.node_memory_pilot_effect.protocols.protocol_memory_pilot import (
    LabModelUnavailableError,
    ProtocolChatAnswerer,
    ProtocolEventPublisher,
    ProtocolFailureRecorder,
    ProtocolMemoryBackend,
)

_CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"
_log = logging.getLogger(__name__)
_NODE = "node_memory_pilot_effect"


def _make_cognee(
    overlay: ModelMemoryPilotOverlay, prompt: str
) -> ProtocolMemoryBackend:
    """Import the optional package's backend only when an overlay names it."""
    from omnimarket.nodes.node_memory_pilot_effect.handlers.backend_cognee import (
        BackendCognee,
    )

    return BackendCognee(overlay, system_prompt=prompt)


class LoggingFailureRecorder:
    """The recorder the runtime gets when none is injected: one error log line per failure.

    The runtime's auto-wiring injects only the event bus, so a handler that needs
    another dependency cannot be built there. A durable failure row is a recorder
    passed in by whoever hosts the node with a store; this one names the failure
    in the log and writes nothing else.
    """

    async def record_failure(
        self, correlation_id: str, error_code: str, detail: str
    ) -> None:
        _log.error(
            "memory pilot failure: correlation_id=%s error_code=%s detail=%s",
            correlation_id,
            error_code,
            detail,
        )


class HandlerMemoryPilot:
    """One request in, a started event and exactly one terminal event out."""

    synchronous_execution: ClassVar[bool] = False

    def __init__(
        self,
        *,
        event_bus: ProtocolEventPublisher,
        failure_recorder: ProtocolFailureRecorder | None = None,
        overlay: ModelMemoryPilotOverlay | None = None,
        backend: ProtocolMemoryBackend | None = None,
        answerer: ProtocolChatAnswerer | None = None,
        contract_path: Path | None = None,
    ) -> None:
        topics = contract_publish_topics(contract_path or _CONTRACT)
        self._started_topic = next(t for t in topics if t.endswith("-started.v1"))
        self._completed_topic = next(t for t in topics if t.endswith("-completed.v1"))
        self._failed_topic = next(t for t in topics if t.endswith("-failed.v1"))
        self._bus = event_bus
        self._recorder = failure_recorder or LoggingFailureRecorder()
        self._overlay = overlay
        self._backend = backend
        self._answerer = answerer

    async def handle(self, request: ModelMemoryPilotRequest) -> ModelMemoryPilotResult:
        started = time.monotonic()
        await self._publish(self._started_topic, request, request.model_dump_json())
        result = await self._run(request, started)
        topic = (
            self._completed_topic
            if result.status is EnumMemoryPilotStatus.COMPLETED
            else self._failed_topic
        )
        if result.error_code is not None:
            await self._record(result)
        await self._publish(topic, request, result.model_dump_json())
        return result

    async def _run(
        self, request: ModelMemoryPilotRequest, started: float
    ) -> ModelMemoryPilotResult:
        def fail(
            code: Code, detail: str, backend: str = "", frozen: str = ""
        ) -> ModelMemoryPilotResult:
            return ModelMemoryPilotResult(
                correlation_id=request.correlation_id,
                operation=request.operation,
                status=EnumMemoryPilotStatus.FAILED,
                reading=EnumMemoryPilotReading.NOT_WORKING,
                backend=backend,
                frozen_hash=frozen,
                error_code=code,
                error_detail=detail,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

        try:
            overlay = load_structured_overlay(
                _NODE, ModelMemoryPilotOverlay, explicit=self._overlay
            )
        except NodeOverlayError as exc:
            return fail(Code.OVERLAY_INVALID, str(exc))
        try:
            frozen = verify_frozen(overlay.pilot_dir, overlay.frozen_hash)
        except PilotNotFrozenError as exc:
            return fail(Code.PILOT_NOT_FROZEN, str(exc), overlay.backend)
        allowed = set(overlay.allowed_hosts)
        for endpoint in overlay.endpoints():
            if endpoint.host not in allowed:
                return fail(
                    Code.ENDPOINT_NOT_ALLOWED, endpoint.host, overlay.backend, frozen
                )
        if (
            self._backend is None
            and BACKEND_REQUIREMENTS[overlay.backend].answerer
            and self._answerer is None
        ):
            return fail(
                Code.BACKEND_ERROR, "no lab answerer injected", overlay.backend, frozen
            )
        if self._backend is None:
            self._backend = self._make_backend(overlay)
        backend = self._backend
        try:
            if request.operation is EnumMemoryPilotOperation.INGEST:
                count = await backend.ingest(request.dataset, request.documents)
                return self._ok(request, backend.name, frozen, started, documents=count)
            reply = await backend.query(request.dataset, request.question)
        except LabModelUnavailableError as exc:
            return fail(Code.LAB_MODEL_UNAVAILABLE, str(exc), backend.name, frozen)
        except KeyError:
            return fail(Code.NOT_INGESTED, request.dataset, backend.name, frozen)
        except Exception as exc:
            return fail(Code.BACKEND_ERROR, type(exc).__name__, backend.name, frozen)
        return self._ok(
            request,
            backend.name,
            frozen,
            started,
            answer=reply.answer,
            context=reply.context,
        )

    def _ok(
        self,
        request: ModelMemoryPilotRequest,
        backend: str,
        frozen: str,
        started: float,
        *,
        documents: int = 0,
        answer: str = "",
        context: tuple[object, ...] = (),
    ) -> ModelMemoryPilotResult:
        return ModelMemoryPilotResult.model_validate(
            {
                "correlation_id": request.correlation_id,
                "operation": request.operation,
                "status": EnumMemoryPilotStatus.COMPLETED,
                "reading": EnumMemoryPilotReading.WORKING,
                "backend": backend,
                "frozen_hash": frozen,
                "documents_ingested": documents,
                "answer": answer,
                "context": context,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
        )

    def _make_backend(self, overlay: ModelMemoryPilotOverlay) -> ProtocolMemoryBackend:
        prompt = frozen_system_prompt(overlay.pilot_dir)
        factories: dict[EnumMemoryPilotBackend, Callable[[], ProtocolMemoryBackend]] = {
            EnumMemoryPilotBackend.IN_MEMORY: BackendInMemory,
            EnumMemoryPilotBackend.LEXICAL_LAB: lambda: BackendInMemory(
                name=EnumMemoryPilotBackend.LEXICAL_LAB.value,
                answerer=self._answerer,
                system_prompt=prompt,
            ),
            EnumMemoryPilotBackend.COGNEE: lambda: _make_cognee(overlay, prompt),
        }
        return factories[overlay.backend]()

    async def _record(self, result: ModelMemoryPilotResult) -> None:
        assert result.error_code is not None
        try:
            await self._recorder.record_failure(
                str(result.correlation_id), result.error_code.value, result.error_detail
            )
        except Exception:
            _log.exception("memory pilot: failure row not recorded")

    async def _publish(
        self, topic: str, request: ModelMemoryPilotRequest, body: str
    ) -> None:
        try:
            await self._bus.publish(
                topic, str(request.correlation_id).encode(), body.encode()
            )
        except Exception:
            _log.exception("memory pilot: failed to publish on %s", topic)


__all__ = ["HandlerMemoryPilot"]
