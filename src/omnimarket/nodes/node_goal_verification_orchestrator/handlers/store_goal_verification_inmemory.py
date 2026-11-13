# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory store of goal verification runs (the default profile; OMN-20033).

A real-backend overlay replaces this class with one that survives a crash. The state this class
holds is exactly what the model's finding F3 says must be durable: the recorded start keys, the
attempt counts, the verdict bindings and the obligation rows.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from omnimarket.nodes.node_goal_verification_orchestrator.models.model_goal_verification import (
    EnumGoalClosureSource,
    EnumGoalVerificationState,
    ModelGoalClosure,
    ModelGoalVerificationRun,
)

_OPEN_STATES = (
    EnumGoalVerificationState.PENDING,
    EnumGoalVerificationState.STARTED,
    EnumGoalVerificationState.EXHAUSTED,
)


class StoreGoalVerificationInMemory:
    """Runs keyed on (goal id, contract revision); dispositions kept apart so they start nothing."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[tuple[UUID, UUID], ModelGoalVerificationRun] = {}
        self._dispositions: dict[tuple[UUID, UUID], tuple[UUID, ...]] = {}

    def record_close(
        self, closure: ModelGoalClosure
    ) -> tuple[ModelGoalVerificationRun, bool]:
        key = (closure.goal_id, closure.contract_revision)
        with self._lock:
            existing = self._runs.get(key)
            if existing is not None:
                if closure.closing_event_id in existing.closing_event_ids:
                    return existing, False
                updated = existing.model_copy(
                    update={
                        "closing_event_ids": (
                            *existing.closing_event_ids,
                            closure.closing_event_id,
                        )
                    }
                )
                self._runs[key] = updated
                return updated, False
            run = ModelGoalVerificationRun(
                goal_id=closure.goal_id,
                contract_revision=closure.contract_revision,
                state=EnumGoalVerificationState.PENDING,
                closing_event_ids=(closure.closing_event_id,),
                disposition_event_ids=self._dispositions.get(key, ()),
                first_closed_at=closure.observed_at,
                delegation_correlation_id=(
                    closure.closing_event_id
                    if closure.source is EnumGoalClosureSource.DELEGATION_TERMINAL
                    else None
                ),
            )
            self._runs[key] = run
            return run, True

    def get_run(
        self, goal_id: UUID, contract_revision: UUID
    ) -> ModelGoalVerificationRun | None:
        with self._lock:
            return self._runs.get((goal_id, contract_revision))

    def mark_attempt(
        self, goal_id: UUID, contract_revision: UUID, at: datetime
    ) -> ModelGoalVerificationRun:
        return self._update(
            goal_id,
            contract_revision,
            lambda run: {
                "state": (
                    run.state
                    if run.state is EnumGoalVerificationState.VERDICT
                    else EnumGoalVerificationState.STARTED
                ),
                "attempts": run.attempts + 1,
                "last_attempt_at": at,
                "obligation_reason": None,
            },
        )

    def mark_unread_attempt(
        self, goal_id: UUID, contract_revision: UUID, at: datetime
    ) -> ModelGoalVerificationRun:
        return self._update(
            goal_id,
            contract_revision,
            lambda run: {"attempts": run.attempts + 1, "last_attempt_at": at},
        )

    def mark_verdict(
        self, goal_id: UUID, contract_revision: UUID, verifier_correlation_id: UUID
    ) -> ModelGoalVerificationRun:
        return self._update(
            goal_id,
            contract_revision,
            lambda _run: {
                "state": EnumGoalVerificationState.VERDICT,
                "verdict_correlation_id": verifier_correlation_id,
                "obligation_reason": None,
            },
        )

    def mark_exhausted(
        self, goal_id: UUID, contract_revision: UUID, reason: str
    ) -> ModelGoalVerificationRun:
        return self._update(
            goal_id,
            contract_revision,
            lambda _run: {
                "state": EnumGoalVerificationState.EXHAUSTED,
                "obligation_reason": reason,
            },
        )

    def record_disposition(
        self, goal_id: UUID, contract_revision: UUID, event_id: UUID
    ) -> bool:
        key = (goal_id, contract_revision)
        with self._lock:
            seen = self._dispositions.get(key, ())
            if event_id in seen:
                return False
            self._dispositions[key] = (*seen, event_id)
            run = self._runs.get(key)
            if run is not None:
                self._runs[key] = run.model_copy(
                    update={"disposition_event_ids": (*seen, event_id)}
                )
            return True

    def list_unverified(self) -> tuple[ModelGoalVerificationRun, ...]:
        with self._lock:
            return tuple(
                run for run in self._runs.values() if run.state in _OPEN_STATES
            )

    def _update(
        self,
        goal_id: UUID,
        contract_revision: UUID,
        changes: Callable[[ModelGoalVerificationRun], dict[str, object]],
    ) -> ModelGoalVerificationRun:
        key = (goal_id, contract_revision)
        with self._lock:
            run = self._runs[key]
            updated = run.model_copy(update=changes(run))
            self._runs[key] = updated
            return updated


__all__: list[str] = ["StoreGoalVerificationInMemory"]
