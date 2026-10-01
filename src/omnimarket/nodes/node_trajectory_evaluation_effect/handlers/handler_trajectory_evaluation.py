# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B handler for node_trajectory_evaluation_effect (OMN-20087).

Order for a submit: read the lane setting (unset means ``off``), validate the
command, check the content mode is permitted by the setting, resolve the
evaluator, run the public-repository guard, take the pacing reservation, submit,
then read status every ``status_poll.interval_seconds`` for at most
``status_poll.max_wait_seconds``. All I/O sits behind injected seams (guard
transport, evaluator, clock), created on first use, so a bare construction does
nothing.

The setting is one of ``off``, ``in_memory`` or ``<external>_metadata`` /
``<external>_full`` where ``<external>`` is a lowercase identifier. External
values are recognised by shape and refused ``backend_unavailable`` until an
adapter registers a concrete evaluator for them.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

import httpx
import yaml

from omnimarket.nodes.node_trajectory_evaluation_effect.evaluators.evaluator_in_memory import (
    EvaluatorInMemory,
)
from omnimarket.nodes.node_trajectory_evaluation_effect.handlers.pacer import Pacer
from omnimarket.nodes.node_trajectory_evaluation_effect.handlers.public_repository_guard import (
    EnumGuardVerdict,
    PublicRepositoryGuard,
    is_valid_repository,
)
from omnimarket.nodes.node_trajectory_evaluation_effect.models.model_trajectory_evaluation import (
    EnumTrajectoryEvaluationReason as Reason,
)
from omnimarket.nodes.node_trajectory_evaluation_effect.models.model_trajectory_evaluation import (
    ModelEvaluationPacing,
    ModelStatusPoll,
    ModelTrajectoryEvaluationAccepted,
    ModelTrajectoryEvaluationFailed,
    ModelTrajectoryEvaluationPoll,
    ModelTrajectoryEvaluationRefused,
    ModelTrajectoryEvaluationStatus,
    ModelTrajectoryEvaluationSubmit,
    ModelWorkUnitScoping,
)
from omnimarket.nodes.node_trajectory_evaluation_effect.protocols import (
    EvaluatorError,
    EvaluatorRateLimitedError,
    EvaluatorRejectedError,
    ModelEvaluatorStatus,
    ModelEvaluatorSubmission,
    ProtocolClock,
    ProtocolTrajectoryEvaluator,
)

_LOG = logging.getLogger(__name__)

SETTING_ENV = "ONEX_TRAJECTORY_EVALUATION_BACKEND"
_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"

_EXTERNAL = re.compile(r"^[a-z][a-z0-9]*_(metadata|full)$")
_RECEIPT_ID = re.compile(r"^[A-Za-z0-9_-]{1,200}$")

_Mode = Literal["meta", "full"]
_MODES: frozenset[str] = frozenset({"meta", "full"})
# Keyed by the setting's suffix; ``in_memory`` permits both modes.
_PERMITTED: Mapping[str, frozenset[str]] = {
    "metadata": frozenset({"meta"}),
    "full": frozenset({"meta", "full"}),
    "in_memory": frozenset({"meta", "full"}),
}


@dataclass(frozen=True)
class _Backend:
    in_memory: bool
    permitted: frozenset[str]


class _SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def utcnow(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def _parse_setting(raw: str | None) -> _Backend | Literal["off"] | None:
    value = (raw or "").strip()
    if value in ("", "off"):
        return "off"
    if value == "in_memory":
        return _Backend(in_memory=True, permitted=_PERMITTED["in_memory"])
    match = _EXTERNAL.fullmatch(value)
    if match is not None:
        return _Backend(in_memory=False, permitted=_PERMITTED[match.group(1)])
    return None


class HandlerTrajectoryEvaluation:
    """Validate, guard, pace and submit one trajectory evaluation."""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        evaluator: ProtocolTrajectoryEvaluator | None = None,
        clock: ProtocolClock | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
        self._scoping = ModelWorkUnitScoping.model_validate(data["work_unit_scoping"])
        self._poll = ModelStatusPoll.model_validate(data["status_poll"])
        pacing = ModelEvaluationPacing.model_validate(data["evaluation_pacing"])
        self._clock: ProtocolClock = clock or _SystemClock()
        self._environ = environ
        self._pacer = Pacer(pacing, self._clock)
        self._guard = PublicRepositoryGuard(
            transport=transport,
            timeout_seconds=self._scoping.visibility_timeout_ms / 1000.0,
        )
        self._in_memory: ProtocolTrajectoryEvaluator = evaluator or EvaluatorInMemory()

    # -- entry ---------------------------------------------------------------

    async def handle(
        self, request: ModelTrajectoryEvaluationSubmit | ModelTrajectoryEvaluationPoll
    ) -> (
        ModelTrajectoryEvaluationAccepted
        | ModelTrajectoryEvaluationRefused
        | ModelTrajectoryEvaluationFailed
        | ModelTrajectoryEvaluationStatus
    ):
        if isinstance(request, ModelTrajectoryEvaluationPoll):
            return await self._handle_poll(request)
        return await self._handle_submit(request)

    # -- submit --------------------------------------------------------------

    async def _handle_submit(
        self, cmd: ModelTrajectoryEvaluationSubmit
    ) -> (
        ModelTrajectoryEvaluationAccepted
        | ModelTrajectoryEvaluationRefused
        | ModelTrajectoryEvaluationFailed
    ):
        cid = cmd.correlation_id
        backend = self._backend(cid)
        if isinstance(backend, ModelTrajectoryEvaluationRefused):
            return backend

        invalid = self._invalid_submit(cmd)
        if invalid is not None:
            return self._refuse(cid, Reason.INPUT_INVALID, invalid)
        mode = cast("_Mode", cmd.content_mode)
        repository = cast("str", cmd.work_unit_repository)

        gate = self._gate_mode(cid, backend, mode)
        if gate is not None:
            return gate

        if self._scoping.require_public_repository:
            verdict = await self._guard.check(repository, cid)
            if verdict is EnumGuardVerdict.NOT_PUBLIC:
                return self._refuse(cid, Reason.REPOSITORY_NOT_PUBLIC)
            if verdict is EnumGuardVerdict.RATE_LIMITED:
                return self._fail(cid, Reason.GUARD_RATE_LIMITED)
            if verdict is EnumGuardVerdict.UNRESOLVED:
                return self._fail(cid, Reason.GUARD_UNRESOLVED)

        refused = self._pacer.reserve(mode)
        if refused is not None:
            return self._refuse(cid, refused)

        key = f"onex-eval-{mode}-{cid}"
        submission = ModelEvaluatorSubmission(
            idempotency_key=key,
            content_mode=mode,
            model_id=cmd.model_id,
            provider=cmd.provider,
            task_type=cmd.task_type,
            prompt_tokens=cmd.prompt_tokens,
            completion_tokens=cmd.completion_tokens,
            latency_ms=cmd.latency_ms,
            quality_score=cmd.quality_score,
            gate_result=cmd.gate_result,
            prompt_text=cmd.prompt_text if mode == "full" else None,
            response_text=cmd.response_text if mode == "full" else None,
        )
        evaluator = self._in_memory
        try:
            async with self._pacer.submit_slot():
                receipt = await evaluator.submit(submission)
        except EvaluatorError as exc:
            self._log(
                "evaluator_submit",
                cid,
                evaluator.host,
                exc.status_code or type(exc).__name__,
            )
            self._pacer.release(mode)
            return self._evaluator_error(cid, exc)
        self._log("evaluator_submit", cid, evaluator.host, "ok")

        status = await self._bounded_poll(cid, evaluator, receipt.receipt_id)
        return ModelTrajectoryEvaluationAccepted(
            correlation_id=cid,
            content_mode=mode,
            idempotency_key=key,
            backend_receipt_id=receipt.receipt_id,
            duplicate=receipt.duplicate,
            verdict_state="completed" if status is not None else "pending",
            verdict=status.verdict if status is not None else None,
        )

    async def _bounded_poll(
        self, cid: UUID, evaluator: ProtocolTrajectoryEvaluator, receipt_id: str
    ) -> ModelEvaluatorStatus | None:
        """Read until terminal: first read now, then every interval, within the bound."""
        for read in range(self._poll.max_reads):
            if read:
                await self._clock.sleep(self._poll.interval_seconds)
            await self._pacer.acquire_status_read()
            try:
                status = await evaluator.read_status(receipt_id)
            except EvaluatorError as exc:
                # The submission is accepted; a failed read leaves it pending
                # for a later poll command rather than resubmitting.
                self._log(
                    "evaluator_status",
                    cid,
                    evaluator.host,
                    exc.status_code or type(exc).__name__,
                )
                return None
            self._log("evaluator_status", cid, evaluator.host, "ok")
            if status.terminal:
                return status
        return None

    # -- poll ----------------------------------------------------------------

    async def _handle_poll(
        self, cmd: ModelTrajectoryEvaluationPoll
    ) -> (
        ModelTrajectoryEvaluationStatus
        | ModelTrajectoryEvaluationRefused
        | ModelTrajectoryEvaluationFailed
    ):
        cid = cmd.correlation_id
        backend = self._backend(cid)
        if isinstance(backend, ModelTrajectoryEvaluationRefused):
            return backend
        if cmd.content_mode not in _MODES:
            return self._refuse(
                cid, Reason.INPUT_INVALID, "content_mode must be meta or full"
            )
        receipt_id = cmd.backend_receipt_id
        if receipt_id is None or _RECEIPT_ID.fullmatch(receipt_id) is None:
            return self._refuse(
                cid,
                Reason.INPUT_INVALID,
                "backend_receipt_id is not a valid receipt id",
            )
        gate = self._gate_mode(cid, backend, cast("_Mode", cmd.content_mode))
        if gate is not None:
            return gate

        evaluator = self._in_memory
        await self._pacer.acquire_status_read()
        try:
            status = await evaluator.read_status(receipt_id)
        except EvaluatorError as exc:
            self._log(
                "evaluator_status",
                cid,
                evaluator.host,
                exc.status_code or type(exc).__name__,
            )
            return self._evaluator_error(cid, exc)
        self._log("evaluator_status", cid, evaluator.host, "ok")
        return ModelTrajectoryEvaluationStatus(
            correlation_id=cid,
            backend_receipt_id=receipt_id,
            verdict_state="completed" if status.terminal else "pending",
            verdict=status.verdict if status.terminal else None,
        )

    # -- shared --------------------------------------------------------------

    def _backend(self, cid: UUID) -> _Backend | ModelTrajectoryEvaluationRefused:
        environ = self._environ if self._environ is not None else os.environ
        parsed = _parse_setting(environ.get(SETTING_ENV))
        if parsed == "off":
            return self._refuse(cid, Reason.BACKEND_DISABLED)
        if parsed is None:
            return self._refuse(cid, Reason.BACKEND_MISCONFIGURED)
        return parsed

    def _gate_mode(
        self, cid: UUID, backend: _Backend, mode: _Mode
    ) -> ModelTrajectoryEvaluationRefused | None:
        """The mode permission, then the evaluator the setting resolves to."""
        if mode not in backend.permitted:
            return self._refuse(
                cid,
                Reason.CONTENT_MODE_NOT_PERMITTED,
                f"content_mode {mode} is not permitted by the configured backend",
            )
        if not backend.in_memory:
            return self._refuse(
                cid,
                Reason.BACKEND_UNAVAILABLE,
                "no evaluator is registered for the configured backend",
            )
        return None

    @staticmethod
    def _invalid_submit(cmd: ModelTrajectoryEvaluationSubmit) -> str | None:
        if cmd.work_unit_repository is None:
            return "work_unit_repository is missing"
        if not is_valid_repository(cmd.work_unit_repository):
            return "work_unit_repository is not an owner/name pair"
        if cmd.content_mode not in _MODES:
            return "content_mode must be meta or full"
        if cmd.content_mode == "full" and (
            cmd.prompt_text is None or cmd.response_text is None
        ):
            return "content_mode full needs prompt_text and response_text"
        return None

    def _evaluator_error(
        self, cid: UUID, exc: EvaluatorError
    ) -> ModelTrajectoryEvaluationRefused | ModelTrajectoryEvaluationFailed:
        if isinstance(exc, EvaluatorRateLimitedError):
            return self._fail(cid, Reason.EVALUATOR_RATE_LIMITED)
        if isinstance(exc, EvaluatorRejectedError):
            if exc.credit_exhausted:
                self._pacer.trip_credit_stop()
            return self._refuse(
                cid, Reason.TARGET_REJECTED, f"status {exc.status_code}"
            )
        return self._fail(cid, Reason.EVALUATOR_TRANSPORT)

    @staticmethod
    def _refuse(
        cid: UUID, reason: Reason, detail: str = ""
    ) -> ModelTrajectoryEvaluationRefused:
        return ModelTrajectoryEvaluationRefused(
            correlation_id=cid, reason=reason, detail=detail
        )

    @staticmethod
    def _fail(cid: UUID, reason: Reason) -> ModelTrajectoryEvaluationFailed:
        return ModelTrajectoryEvaluationFailed(correlation_id=cid, reason=reason)

    @staticmethod
    def _log(operation: str, cid: UUID, host: str, status: object) -> None:
        # Never a header or a body.
        _LOG.info(
            "outbound_call operation=%s correlation_id=%s host=%s status=%s",
            operation,
            cid,
            host,
            status,
        )
