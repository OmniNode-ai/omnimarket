# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Number each step of one delegation run, in the order the orchestrator takes it.

One recorder lives for one ``dispatch``. Its counter is the run's only ordering
authority: every step takes the next number at the moment the orchestrator
reaches it, and that number travels on the step event. Nothing downstream may
order a run by arrival or by a clock.

Delivery is best-effort and never part of the delegation: the number is taken
first, then the step is written to the local store and published, each under its
own guard. A step that cannot be delivered leaves a gap in what the page shows,
never a renumbered run and never a failed delegation.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Protocol
from uuid import UUID

import yaml

from omnimarket.enums.enum_run_step_kind import EnumRunStepKind
from omnimarket.enums.enum_run_step_outcome import EnumRunStepOutcome
from omnimarket.models.model_delegation_run_step import ModelDelegationRunStep
from omnimarket.nodes.node_projection_run_trace.handlers.handler_projection_run_trace import (
    HandlerProjectionRunTrace,
)
from omnimarket.nodes.node_projection_run_trace.handlers.handler_run_trace_store import (
    apply_run_step,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

logger = logging.getLogger(__name__)

# A step's free text is a short explanation, not a transcript.
_REASON_LIMIT = 500


@dataclass(frozen=True)
class _PendingStep:
    kind: EnumRunStepKind
    started_at: datetime
    started: float
    backend_id: str
    model_id: str | None


class ProtocolRunStepPublisher(Protocol):
    def publish(
        self,
        *,
        topic: str,
        event_type: str,
        payload: Mapping[str, object],
        event_id: str,
        correlation_id: str | None = None,
        partition_key: str | None = None,
    ) -> bool:
        """Publish one event. The recorder reads no result; a raise is logged and the run goes on."""


@cache
def run_step_topic() -> str | None:
    """This node's declared step topic, read from its own contract."""
    contract = Path(__file__).resolve().parent / "contract.yaml"
    try:
        raw = yaml.safe_load(contract.read_text(encoding="utf-8"))
        return str(raw["runtime_dispatch"]["run_step_event"])
    except Exception:
        logger.warning("delegate-skill run-step topic unresolved from %s", contract)
        return None


def _short(text: str | None) -> str | None:
    if not text:
        return None
    return text if len(text) <= _REASON_LIMIT else text[: _REASON_LIMIT - 1] + "…"


class RunStepRecorder:
    """The step counter of one run, and the two places its steps are delivered."""

    def __init__(
        self,
        *,
        tenant_id: str,
        correlation_id: UUID,
        publisher: ProtocolRunStepPublisher | None,
        store: DatabaseAdapter | None,
    ) -> None:
        self._tenant_id = tenant_id
        self._correlation_id = correlation_id
        self._publisher = publisher
        self._store = store
        self._topic = run_step_topic() if publisher is not None else None
        self._next_sequence = 1
        self._calls = 0
        self._started_at = datetime.now(UTC)
        self._started = time.monotonic()
        self._routed_backend_id: str | None = None
        self._last_failed_sequence: int | None = None
        self._last_failure_cause: str | None = None
        self._terminated = False
        self._pending: _PendingStep | None = None

    # --- the steps a run can take -------------------------------------------

    def request(self, *, task_type: str) -> int:
        return self._record(
            EnumRunStepKind.REQUEST,
            started_at=self._started_at,
            outcome=EnumRunStepOutcome.SUCCEEDED,
            reason=task_type,
        )

    def routing(
        self,
        *,
        backend_id: str,
        model_id: str,
        tier: str,
        candidates: Sequence[str] = (),
    ) -> int | None:
        """A backend was chosen. A retry on the backend already chosen is no decision."""
        if backend_id == self._routed_backend_id:
            return None
        previous = self._routed_backend_id
        self._routed_backend_id = backend_id
        if previous is None:
            reason = f"first choice: tier {tier}"
        else:
            cause = self._last_failure_cause or "the previous attempt"
            reason = f"tier {tier}, after {cause} on {previous}"
        return self._record(
            EnumRunStepKind.ROUTING_DECISION,
            started_at=datetime.now(UTC),
            outcome=EnumRunStepOutcome.SUCCEEDED,
            backend_id=backend_id,
            model_id=model_id,
            reason=reason,
            candidates=tuple(candidates),
        )

    def provider_call(
        self,
        *,
        started_at: datetime,
        duration_ms: int | None,
        backend_id: str,
        model_id: str | None,
        failure_cause: str | None = None,
        reason: str | None = None,
    ) -> int:
        self._pending = None
        self._calls += 1
        return self._record(
            EnumRunStepKind.PROVIDER_CALL,
            started_at=started_at,
            duration_ms=duration_ms,
            outcome=(
                EnumRunStepOutcome.FAILED
                if failure_cause is not None
                else EnumRunStepOutcome.SUCCEEDED
            ),
            failure_cause=failure_cause,
            backend_id=backend_id,
            model_id=model_id,
            attempt_number=self._calls,
            reason=reason,
        )

    def quality_gate(
        self,
        *,
        answered_at: datetime | None = None,
        backend_id: str,
        failure_cause: str | None = None,
        reason: str | None = None,
    ) -> int:
        """The gate's verdict on the latest call; timed from the provider's answer."""
        if answered_at is None:
            answered_at = (
                self._pending.started_at
                if self._pending is not None
                else datetime.now(UTC)
            )
        self._pending = None
        elapsed = datetime.now(UTC) - answered_at
        return self._record(
            EnumRunStepKind.QUALITY_GATE,
            started_at=answered_at,
            duration_ms=max(0, int(elapsed / timedelta(milliseconds=1))),
            outcome=(
                EnumRunStepOutcome.FAILED
                if failure_cause is not None
                else EnumRunStepOutcome.SUCCEEDED
            ),
            failure_cause=failure_cause,
            backend_id=backend_id,
            attempt_number=self._calls or None,
            reason=reason,
        )

    def begin_provider_call(
        self, *, started_at: datetime, backend_id: str, model_id: str
    ) -> None:
        self._pending = _PendingStep(
            EnumRunStepKind.PROVIDER_CALL,
            started_at,
            time.monotonic(),
            backend_id,
            model_id,
        )

    def begin_quality_gate(self, *, backend_id: str) -> None:
        self._pending = _PendingStep(
            EnumRunStepKind.QUALITY_GATE,
            datetime.now(UTC),
            time.monotonic(),
            backend_id,
            None,
        )

    def interrupt(
        self,
        cause: str,
        reason: str,
        *,
        prior_calls: Sequence[tuple[str, str, str]] = (),
        interrupted_model: str | None = None,
        provider_call_in_flight: bool = True,
    ) -> None:
        """Finish the interrupted step and this same recorder's run once."""
        if self._terminated:
            return
        pending = self._pending
        if pending is not None and pending.kind is EnumRunStepKind.PROVIDER_CALL:
            for model, failure, detail in prior_calls:
                self.provider_call(
                    started_at=pending.started_at,
                    duration_ms=None,
                    backend_id=pending.backend_id,
                    model_id=model,
                    failure_cause=failure,
                    reason=detail,
                )
            if provider_call_in_flight:
                self.provider_call(
                    started_at=pending.started_at,
                    duration_ms=max(
                        0, int((time.monotonic() - pending.started) * 1000)
                    ),
                    backend_id=pending.backend_id,
                    model_id=interrupted_model or pending.model_id,
                    failure_cause=cause,
                    reason=reason,
                )
        elif pending is not None:
            self.quality_gate(
                answered_at=pending.started_at,
                backend_id=pending.backend_id,
                failure_cause=cause,
                reason=reason,
            )
        self.terminal(
            succeeded=False,
            backend_id=None if pending is None else pending.backend_id,
            model_id=interrupted_model
            or (None if pending is None else pending.model_id),
            failure_cause=cause,
        )

    def terminal(
        self,
        *,
        succeeded: bool,
        backend_id: str | None = None,
        model_id: str | None = None,
        failure_cause: str | None = None,
    ) -> int | None:
        """The run ended. A failed run names the earlier step that failed it."""
        if self._terminated:
            return None
        self._terminated = True
        cause = None if succeeded else (failure_cause or self._last_failure_cause)
        return self._record(
            EnumRunStepKind.TERMINAL,
            started_at=datetime.now(UTC),
            duration_ms=int((time.monotonic() - self._started) * 1000),
            outcome=(
                EnumRunStepOutcome.SUCCEEDED if succeeded else EnumRunStepOutcome.FAILED
            ),
            failure_cause=cause or (None if succeeded else "failed"),
            backend_id=backend_id,
            model_id=model_id,
            failing_step_sequence=None if succeeded else self._last_failed_sequence,
        )

    # --- numbering and delivery ---------------------------------------------

    def _record(
        self,
        kind: EnumRunStepKind,
        *,
        started_at: datetime,
        outcome: EnumRunStepOutcome,
        duration_ms: int | None = None,
        failure_cause: str | None = None,
        backend_id: str | None = None,
        model_id: str | None = None,
        attempt_number: int | None = None,
        reason: str | None = None,
        candidates: tuple[str, ...] = (),
        failing_step_sequence: int | None = None,
    ) -> int:
        # The number is taken before anything can fail, so a step that cannot be
        # built or delivered still holds its place in the run.
        sequence = self._next_sequence
        self._next_sequence += 1
        if (
            outcome is EnumRunStepOutcome.FAILED
            and kind is not EnumRunStepKind.TERMINAL
        ):
            self._last_failed_sequence = sequence
            self._last_failure_cause = failure_cause
        try:
            step = ModelDelegationRunStep(
                tenant_id=self._tenant_id,
                correlation_id=self._correlation_id,
                step_sequence=sequence,
                step_kind=kind,
                started_at=started_at,
                duration_ms=duration_ms,
                outcome=outcome,
                failure_cause=failure_cause,
                backend_id=backend_id,
                model_id=model_id,
                attempt_number=attempt_number,
                reason=_short(reason),
                candidates=candidates,
                failing_step_sequence=failing_step_sequence,
            )
        except Exception:
            logger.warning(
                "run step %d (%s) of correlation_id=%s could not be built",
                sequence,
                kind.value,
                self._correlation_id,
                exc_info=True,
            )
            return sequence
        self._store_step(step)
        self._publish_step(step)
        return sequence

    def _store_step(self, step: ModelDelegationRunStep) -> None:
        if self._store is None:
            return
        try:
            apply_run_step(HandlerProjectionRunTrace().handle(step), self._store)
        except Exception:
            logger.warning(
                "run step %d of correlation_id=%s was not written to the local store",
                step.step_sequence,
                self._correlation_id,
                exc_info=True,
            )

    def _publish_step(self, step: ModelDelegationRunStep) -> None:
        if self._publisher is None or self._topic is None:
            return
        correlation = str(self._correlation_id)
        try:
            self._publisher.publish(
                topic=self._topic,
                event_type="delegation.run_step",
                payload=step.model_dump(mode="json"),
                event_id=f"delegation-run-step-{correlation}-{step.step_sequence}",
                correlation_id=correlation,
                partition_key=correlation,
            )
        except Exception:
            logger.warning(
                "run step %d of correlation_id=%s was not published",
                step.step_sequence,
                self._correlation_id,
                exc_info=True,
            )


__all__ = ["ProtocolRunStepPublisher", "RunStepRecorder", "run_step_topic"]
