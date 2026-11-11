# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The daily memory pilot run: the frozen question set asked of the baseline and the candidate, measured.

Both arms read the same corpus and answer the same questions with the frozen prompt on lab models only. The run
publishes a started event and exactly one completed or failed event, and writes the measured metrics to the
measurement node's result port. It holds no threshold: the pilot's frozen bar is the measurement node's.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.contract_topics import contract_topic
from omnimarket.nodes.node_memory_pilot_effect.handlers.backend_in_memory import (
    BackendInMemory,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.connection_audit import (
    SocketConnectionAudit,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.frozen_pilot import (
    PilotNotFrozenError,
    frozen_system_prompt,
    verify_frozen,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.handler_memory_pilot import (
    LoggingFailureRecorder,
    _make_cognee,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.lab_answerer import (
    LabChatAnswerer,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.pilot_run_metrics import (
    ModelQuestionTrial,
    p95_seconds,
    run_metrics,
    score_arm,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.pilot_scoring import (
    ModelPilotQuestionSet,
    load_questions,
    score_answer,
)
from omnimarket.nodes.node_memory_pilot_effect.handlers.result_file_sink import (
    FileResultSink,
)
from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot import (
    ModelMemoryPilotDocument,
    ModelMemoryPilotOverlay,
)
from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot_run import (
    EnumMemoryPilotRunErrorCode as Code,
)
from omnimarket.nodes.node_memory_pilot_effect.models.model_memory_pilot_run import (
    ModelMemoryPilotRunRequest,
    ModelMemoryPilotRunResult,
)
from omnimarket.nodes.node_memory_pilot_effect.protocols.protocol_memory_pilot import (
    LabModelUnavailableError,
    ProtocolChatAnswerer,
    ProtocolConnectionAudit,
    ProtocolEventPublisher,
    ProtocolFailureRecorder,
    ProtocolMemoryBackend,
    ProtocolPilotResultSink,
)

_CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"
_log = logging.getLogger(__name__)
_NODE = "node_memory_pilot_effect"
_PLANTED_DIR_PREFIX = "planted_notes/"

CandidateFactory = Callable[[ModelMemoryPilotOverlay, str], ProtocolMemoryBackend]


class RunAbortError(Exception):
    """A typed end of the run, carrying what to say and whether a partial result is written."""

    def __init__(
        self,
        code: Code,
        detail: str,
        partial: dict[str, float] | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.partial = partial


class CorpusMismatchError(Exception):
    """A corpus file is absent or differs from the frozen manifest."""


def load_corpus(
    pilot_dir: Path, corpus_root: Path
) -> tuple[ModelMemoryPilotDocument, ...]:
    """The corpus the frozen manifest lists, each file hash-checked, in manifest order."""
    manifest = json.loads((pilot_dir / "corpus_manifest.json").read_text("utf-8"))
    documents: list[ModelMemoryPilotDocument] = []
    for entry in manifest["files"]:
        path = str(entry["path"])
        base = pilot_dir if path.startswith(_PLANTED_DIR_PREFIX) else corpus_root
        target = base / path
        try:
            data = target.read_bytes()
        except OSError:
            raise CorpusMismatchError(f"{path} is unreadable") from None
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise CorpusMismatchError(f"{path} differs from the frozen manifest")
        documents.append(
            ModelMemoryPilotDocument(doc_id=path, text=data.decode("utf-8"))
        )
    return tuple(documents)


class HandlerMemoryPilotRun:
    """One request in, a started event and exactly one terminal event out; the measured result written once."""

    synchronous_execution: ClassVar[bool] = False

    def __init__(
        self,
        *,
        event_bus: ProtocolEventPublisher,
        overlay: ModelMemoryPilotOverlay | None = None,
        answerer: ProtocolChatAnswerer | None = None,
        result_sink: ProtocolPilotResultSink | None = None,
        connection_audit: ProtocolConnectionAudit | None = None,
        candidate_factory: CandidateFactory | None = None,
        failure_recorder: ProtocolFailureRecorder | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        contract_path: Path | None = None,
    ) -> None:
        path = contract_path or _CONTRACT
        self._started = contract_topic(path, "publish_topics", "-run-started.v1")
        self._completed = contract_topic(path, "publish_topics", "-run-completed.v1")
        self._failed = contract_topic(path, "publish_topics", "-run-failed.v1")
        self._bus = event_bus
        self._overlay = overlay
        self._answerer = answerer
        self._sink = result_sink
        self._audit = connection_audit
        self._candidate_factory = candidate_factory
        self._recorder = failure_recorder or LoggingFailureRecorder()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic

    async def handle(
        self, request: ModelMemoryPilotRunRequest
    ) -> ModelMemoryPilotRunResult:
        started = self._monotonic()
        await self._publish(self._started, request, request.model_dump_json())
        result = await self._run(request, started)
        if result.error_code is not None:
            try:
                await self._recorder.record_failure(
                    str(request.correlation_id),
                    result.error_code.value,
                    result.error_detail,
                )
            except Exception:
                _log.exception("memory pilot run: failure row not recorded")
        topic = self._completed if result.status == "completed" else self._failed
        await self._publish(topic, request, result.model_dump_json())
        return result

    def _failure(
        self,
        request: ModelMemoryPilotRunRequest,
        started: float,
        code: Code,
        detail: str,
        *,
        frozen: str = "",
        written: bool = False,
        ingest_seconds: float = 0.0,
        outside: tuple[str, ...] = (),
        metrics: dict[str, float] | None = None,
    ) -> ModelMemoryPilotRunResult:
        return ModelMemoryPilotRunResult(
            correlation_id=request.correlation_id,
            status="failed",
            error_code=code,
            error_detail=detail,
            frozen_hash=frozen,
            metrics=metrics or {},
            ingest_seconds=ingest_seconds,
            outside_connections=outside,
            result_written=written,
            elapsed_seconds=self._monotonic() - started,
        )

    async def _run(
        self, request: ModelMemoryPilotRunRequest, started: float
    ) -> ModelMemoryPilotRunResult:
        try:
            overlay = load_structured_overlay(
                _NODE, ModelMemoryPilotOverlay, explicit=self._overlay
            )
        except NodeOverlayError as exc:
            return self._failure(request, started, Code.OVERLAY_INVALID, str(exc))
        settings = overlay.run
        if settings is None:
            return self._failure(
                request, started, Code.OVERLAY_INVALID, "run settings absent"
            )
        try:
            frozen = verify_frozen(overlay.pilot_dir, overlay.frozen_hash)
        except PilotNotFrozenError as exc:
            return self._failure(request, started, Code.PILOT_NOT_FROZEN, str(exc))
        allowed = set(overlay.allowed_hosts)
        for endpoint in overlay.endpoints():
            if endpoint.host not in allowed:
                return self._failure(
                    request,
                    started,
                    Code.ENDPOINT_NOT_ALLOWED,
                    endpoint.host,
                    frozen=frozen,
                )
        try:
            documents = load_corpus(overlay.pilot_dir, settings.corpus_root)
        except CorpusMismatchError as exc:
            return self._failure(
                request, started, Code.CORPUS_MISMATCH, str(exc), frozen=frozen
            )
        questions = load_questions(overlay.pilot_dir)
        prompt = frozen_system_prompt(overlay.pilot_dir)
        audit = self._audit or SocketConnectionAudit()
        audit.start(overlay.allowed_hosts)
        outside: tuple[str, ...] = ()
        try:
            baseline_trials, candidate_trials, ingest_seconds = await self._arms(
                overlay, request, documents, questions, prompt
            )
        except RunAbortError as abort:
            outside = audit.stop()
            written = False
            if abort.partial is not None:
                partial = {
                    **abort.partial,
                    settings.metric_names.outside_connections: float(len(outside)),
                }
                written = self._write(overlay, partial) is None
                return self._failure(
                    request,
                    started,
                    abort.code,
                    abort.detail,
                    frozen=frozen,
                    written=written,
                    outside=outside,
                    metrics=partial,
                )
            return self._failure(
                request,
                started,
                abort.code,
                abort.detail,
                frozen=frozen,
                outside=outside,
            )
        outside = audit.stop()
        baseline = score_arm("baseline", baseline_trials)
        candidate = score_arm("candidate", candidate_trials)
        p95 = p95_seconds([t.latency_seconds for t in candidate_trials])
        metrics = run_metrics(
            settings.metric_names,
            baseline,
            candidate,
            ingest_seconds=ingest_seconds,
            failed_items=0,
            p95=p95,
            outside_connections=len(outside),
        )
        problem = self._write(overlay, metrics)
        if problem is not None:
            return self._failure(
                request,
                started,
                Code.RESULT_NOT_WRITTEN,
                problem,
                frozen=frozen,
                ingest_seconds=ingest_seconds,
                outside=outside,
                metrics=metrics,
            )
        return ModelMemoryPilotRunResult(
            correlation_id=request.correlation_id,
            status="completed",
            frozen_hash=frozen,
            baseline=baseline,
            candidate=candidate,
            metrics=metrics,
            ingest_seconds=ingest_seconds,
            query_p95_seconds=p95,
            outside_connections=outside,
            result_written=True,
            elapsed_seconds=self._monotonic() - started,
        )

    def _write(
        self, overlay: ModelMemoryPilotOverlay, metrics: dict[str, float]
    ) -> str | None:
        assert overlay.run is not None
        sink = self._sink or FileResultSink(overlay.run.result_file)
        try:
            sink.write(self._clock(), metrics)
        except OSError as exc:
            return type(exc).__name__
        return None

    async def _arms(
        self,
        overlay: ModelMemoryPilotOverlay,
        request: ModelMemoryPilotRunRequest,
        documents: tuple[ModelMemoryPilotDocument, ...],
        questions: ModelPilotQuestionSet,
        prompt: str,
    ) -> tuple[list[ModelQuestionTrial], list[ModelQuestionTrial], float]:
        assert overlay.run is not None
        run = overlay.run
        answerer = self._answerer
        if answerer is None:
            if overlay.chat is None:
                raise RunAbortError(
                    Code.OVERLAY_INVALID, "no chat endpoint for the baseline"
                )
            answerer = LabChatAnswerer(
                overlay.chat,
                temperature=overlay.temperature,
                max_completion_tokens=overlay.max_completion_tokens,
            )
        baseline_backend = BackendInMemory(
            name="lexical_lab", answerer=answerer, system_prompt=prompt
        )
        run_overlay = overlay.model_copy(
            update={
                "storage_root": (overlay.storage_root or Path("."))
                / run.runs_directory
                / str(request.correlation_id)
            }
        )
        factory = self._candidate_factory or _make_cognee
        candidate_backend = factory(run_overlay, prompt)
        dataset = run.dataset

        async def baseline_arm() -> list[ModelQuestionTrial]:
            await baseline_backend.ingest(dataset, documents)
            return await self._ask_all(
                baseline_backend, dataset, questions, run.trials, Code.BASELINE_FAILED
            )

        async def candidate_arm() -> tuple[list[ModelQuestionTrial], float]:
            began = self._monotonic()
            try:
                await candidate_backend.ingest(dataset, documents)
            except LabModelUnavailableError as exc:
                raise RunAbortError(Code.LAB_MODEL_UNAVAILABLE, str(exc)) from None
            except Exception as exc:
                seconds = self._monotonic() - began
                raise RunAbortError(
                    Code.INGEST_FAILED,
                    type(exc).__name__,
                    partial={
                        run.metric_names.ingest_seconds: seconds,
                        run.metric_names.failed_items: float(len(documents)),
                    },
                ) from None
            seconds = self._monotonic() - began
            trials = await self._ask_all(
                candidate_backend, dataset, questions, run.trials, Code.INGEST_FAILED
            )
            return trials, seconds

        if run.parallel_arms:
            gathered = await asyncio.gather(
                baseline_arm(), candidate_arm(), return_exceptions=True
            )
            for item in gathered:
                if isinstance(item, RunAbortError):
                    raise item
                if isinstance(item, BaseException):
                    raise item
            baseline_trials = gathered[0]
            candidate_result = gathered[1]
            assert isinstance(baseline_trials, list)
            assert isinstance(candidate_result, tuple)
            return baseline_trials, candidate_result[0], candidate_result[1]
        baseline_trials = await baseline_arm()
        candidate_trials, seconds = await candidate_arm()
        return baseline_trials, candidate_trials, seconds

    async def _ask_all(
        self,
        backend: ProtocolMemoryBackend,
        dataset: str,
        questions: ModelPilotQuestionSet,
        trials: int,
        _code: Code,
    ) -> list[ModelQuestionTrial]:
        recorded: list[ModelQuestionTrial] = []
        for trial in range(1, trials + 1):
            for question in questions.questions:
                began = self._monotonic()
                errored = False
                correct = False
                try:
                    reply = await backend.query(dataset, question.question)
                    correct = score_answer(
                        question, reply.answer, questions.abstain_regex
                    )
                except LabModelUnavailableError as exc:
                    raise RunAbortError(Code.LAB_MODEL_UNAVAILABLE, str(exc)) from None
                except Exception:
                    errored = True
                recorded.append(
                    ModelQuestionTrial(
                        question_id=question.id,
                        category=question.category,
                        trial=trial,
                        correct=correct,
                        latency_seconds=self._monotonic() - began,
                        errored=errored,
                    )
                )
        return recorded

    async def _publish(
        self, topic: str, request: ModelMemoryPilotRunRequest, body: str
    ) -> None:
        try:
            await self._bus.publish(
                topic, str(request.correlation_id).encode(), body.encode()
            )
        except Exception:
            _log.exception("memory pilot run: failed to publish on %s", topic)


__all__ = ["CorpusMismatchError", "HandlerMemoryPilotRun", "load_corpus"]
