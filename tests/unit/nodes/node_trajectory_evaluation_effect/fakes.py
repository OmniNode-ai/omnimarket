# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic seams for the trajectory evaluation tests (OMN-20087)."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import httpx

from omnimarket.nodes.node_trajectory_evaluation_effect.evaluators.evaluator_in_memory import (
    EvaluatorInMemory,
)
from omnimarket.nodes.node_trajectory_evaluation_effect.handlers.handler_trajectory_evaluation import (
    HandlerTrajectoryEvaluation,
)
from omnimarket.nodes.node_trajectory_evaluation_effect.models.model_trajectory_evaluation import (
    ModelTrajectoryEvaluationPoll,
    ModelTrajectoryEvaluationSubmit,
)
from omnimarket.nodes.node_trajectory_evaluation_effect.protocols import (
    ModelEvaluatorReceipt,
    ModelEvaluatorStatus,
    ModelEvaluatorSubmission,
)

SETTING = "ONEX_TRAJECTORY_EVALUATION_BACKEND"
REPO = "OmniNode-ai/omnimarket"
START = dt.datetime(2026, 9, 30, 12, 0, tzinfo=dt.UTC)


class FakeClock:
    """Sleeping advances time and yields; nothing ever really sleeps."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def utcnow(self) -> dt.datetime:
        return START + dt.timedelta(seconds=self.t)

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds
        await asyncio.sleep(0)


class RecordingEvaluator(EvaluatorInMemory):
    """In-memory evaluator that records calls, concurrency and call times."""

    def __init__(self, clock: FakeClock | None = None, **kw: Any) -> None:
        super().__init__(**kw)
        self.clock = clock
        self.submissions: list[ModelEvaluatorSubmission] = []
        self.submit_times: list[float] = []
        self.read_times: list[float] = []
        self.reads = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.fail_with: Exception | None = None

    def _now(self) -> float:
        return self.clock.t if self.clock else 0.0

    async def submit(
        self, submission: ModelEvaluatorSubmission
    ) -> ModelEvaluatorReceipt:
        self.submissions.append(submission)
        self.submit_times.append(self._now())
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            if self.fail_with is not None:
                raise self.fail_with
            return await super().submit(submission)
        finally:
            self.in_flight -= 1

    async def read_status(self, receipt_id: str) -> ModelEvaluatorStatus:
        self.reads += 1
        self.read_times.append(self._now())
        return await super().read_status(receipt_id)


class Github:
    """Records anonymous repository reads and answers from a fixed reply."""

    def __init__(self, reply: Callable[[httpx.Request], httpx.Response] | None = None):
        self.requests: list[httpx.Request] = []
        self._reply = reply or (lambda _r: httpx.Response(200, json={"private": False}))

    def transport(self) -> httpx.MockTransport:
        def _handle(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            return self._reply(request)

        return httpx.MockTransport(_handle)


def make_handler(
    setting: str | None = "in_memory",
    *,
    github: Github | None = None,
    evaluator: EvaluatorInMemory | None = None,
    clock: FakeClock | None = None,
) -> tuple[HandlerTrajectoryEvaluation, Github, RecordingEvaluator, FakeClock]:
    github = github or Github()
    clock = clock or FakeClock()
    ev = evaluator or RecordingEvaluator(clock)
    handler = HandlerTrajectoryEvaluation(
        transport=github.transport(),
        evaluator=ev,
        clock=clock,
        environ={} if setting is None else {SETTING: setting},
    )
    assert isinstance(ev, RecordingEvaluator)
    return handler, github, ev, clock


def submit_cmd(**overrides: Any) -> ModelTrajectoryEvaluationSubmit:
    fields: dict[str, Any] = {
        "correlation_id": uuid4(),
        "work_unit_repository": REPO,
        "content_mode": "meta",
        "requested_at": "2026-09-30T12:00:00Z",
        "model_id": "model-a",
        "provider": "provider-a",
        "task_type": "code_generation",
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "latency_ms": 300,
        "quality_score": 0.8,
        "gate_result": "pass",
        "prompt_text": None,
        "response_text": None,
    }
    fields.update(overrides)
    return ModelTrajectoryEvaluationSubmit.model_validate(fields)


def full_cmd(**overrides: Any) -> ModelTrajectoryEvaluationSubmit:
    return submit_cmd(
        content_mode="full", prompt_text="p", response_text="r", **overrides
    )


def poll_cmd(**overrides: Any) -> ModelTrajectoryEvaluationPoll:
    fields: dict[str, Any] = {
        "correlation_id": uuid4(),
        "content_mode": "meta",
        "backend_receipt_id": "mem-abc123",
    }
    fields.update(overrides)
    return ModelTrajectoryEvaluationPoll.model_validate(fields)
