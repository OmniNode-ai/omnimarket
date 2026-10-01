# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Node-local seams for node_trajectory_evaluation_effect (OMN-20087).

The evaluator is behind ``ProtocolTrajectoryEvaluator``; time (reads of the
clock and sleeping) is behind ``ProtocolClock``. The handler never touches
either directly, so tests are deterministic and never really sleep.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class ModelEvaluatorSubmission(BaseModel):
    """What the evaluator receives for one submission."""

    model_config = ConfigDict(frozen=True)

    idempotency_key: str
    content_mode: Literal["meta", "full"]
    model_id: str | None = None
    provider: str | None = None
    task_type: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_ms: int | None = None
    quality_score: float | None = None
    gate_result: str | None = None
    prompt_text: str | None = None
    response_text: str | None = None


class ModelEvaluatorReceipt(BaseModel):
    """The evaluator's receipt for a submission."""

    model_config = ConfigDict(frozen=True)

    receipt_id: str
    duplicate: bool


class ModelEvaluatorStatus(BaseModel):
    """One status read of a receipt."""

    model_config = ConfigDict(frozen=True)

    terminal: bool
    verdict: str | None = None


class EvaluatorError(Exception):
    """Base of the evaluator's typed errors."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class EvaluatorRateLimitedError(EvaluatorError):
    """The evaluator throttled the call. Retryable."""


class EvaluatorRejectedError(EvaluatorError):
    """The evaluator refused the call. Not retried against the same backend."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        credit_exhausted: bool = False,
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.credit_exhausted = credit_exhausted


class EvaluatorTransportError(EvaluatorError):
    """The call did not complete (connection, timeout, bad response). Retryable."""


@runtime_checkable
class ProtocolTrajectoryEvaluator(Protocol):
    """An evaluation backend."""

    @property
    def host(self) -> str:
        """The host the backend is reached at, for the structured log line."""
        ...

    async def submit(
        self, submission: ModelEvaluatorSubmission
    ) -> ModelEvaluatorReceipt: ...

    async def read_status(self, receipt_id: str) -> ModelEvaluatorStatus: ...


class ProtocolClock(Protocol):
    """Clock and sleeper seam."""

    def monotonic(self) -> float: ...

    def utcnow(self) -> dt.datetime: ...

    async def sleep(self, seconds: float) -> None: ...
