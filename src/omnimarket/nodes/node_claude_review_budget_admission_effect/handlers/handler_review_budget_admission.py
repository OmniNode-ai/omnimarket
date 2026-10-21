# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Atomic persistence boundary for review admission commands from synchronous and Batch callers.

Run one budget authority for all keys on the bus, backed by one persistent database.
BEGIN IMMEDIATE serializes the entire read/reduce/write across connections and processes;
no positive decision is returned until COMMIT. A failed store raises, never grants.
The caller must receive a positive `started` decision before dispatching upstream.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from omnimarket.models.claude_review_budget.model_review_budget_admission import (
    ModelReviewBudgetCommand,
    ModelReviewBudgetDecision,
    ModelReviewBudgetPolicy,
    ModelReviewBudgetReduction,
    ModelReviewBudgetState,
    ModelReviewBudgetTransition,
)
from omnimarket.nodes.node_claude_review_budget_compute import HandlerClaudeReviewBudget
from omnimarket.nodes.node_claude_review_budget_reducer import (
    HandlerClaudeReviewBudgetReducer,
)


class ProtocolReviewBudgetStore(Protocol):
    def transact(
        self, reduce: Callable[[ModelReviewBudgetState], ModelReviewBudgetReduction]
    ) -> ModelReviewBudgetReduction:
        """Commit the transition atomically or raise; never return an uncommitted grant."""
        ...


class SqliteReviewBudgetStore:
    """Persistent authority store. All workers for an aggregate use the same DB file."""

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        if self._path == ":memory:":
            raise ValueError("admission requires durable storage")
        with sqlite3.connect(self._path, timeout=30) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS review_budget_authority "
                "(id INTEGER PRIMARY KEY CHECK(id = 1), state_json TEXT NOT NULL)"
            )

    def transact(
        self, reduce: Callable[[ModelReviewBudgetState], ModelReviewBudgetReduction]
    ) -> ModelReviewBudgetReduction:
        connection = sqlite3.connect(self._path, timeout=30)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state_json FROM review_budget_authority WHERE id = 1"
            ).fetchone()
            state = (
                ModelReviewBudgetState.model_validate_json(row[0])
                if row
                else ModelReviewBudgetState()
            )
            reduced = reduce(state)
            connection.execute(
                "INSERT INTO review_budget_authority (id, state_json) VALUES (1, ?) "
                "ON CONFLICT(id) DO UPDATE SET state_json = excluded.state_json",
                (reduced.state.model_dump_json(),),
            )
            connection.commit()
            return reduced
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def _clock() -> datetime:
    return datetime.now(UTC)


class HandlerClaudeReviewBudgetAdmission:
    """Bus handler and durable admission authority; the overlay is server owned."""

    def __init__(
        self,
        *,
        store: ProtocolReviewBudgetStore | None = None,
        policy: ModelReviewBudgetPolicy | None = None,
        pricing: HandlerClaudeReviewBudget | None = None,
        clock: Callable[[], datetime] = _clock,
    ) -> None:
        if store is None:
            store_path = os.environ.get("CLAUDE_REVIEW_BUDGET_DB")
            if not store_path:
                raise ValueError(
                    "CLAUDE_REVIEW_BUDGET_DB must name the persistent authority database"
                )
            store = SqliteReviewBudgetStore(store_path)
        if policy is None:
            import yaml

            policy_path = os.environ.get("CLAUDE_REVIEW_BUDGET_POLICY")
            if not policy_path:
                raise ValueError(
                    "CLAUDE_REVIEW_BUDGET_POLICY must name the versioned server overlay"
                )
            policy = ModelReviewBudgetPolicy.model_validate(
                yaml.safe_load(Path(policy_path).read_text())
            )
        self._store, self._policy, self._clock = (
            store,
            policy.model_copy(deep=True),
            clock,
        )
        self._pricing = pricing or HandlerClaudeReviewBudget()
        self._reducer = HandlerClaudeReviewBudgetReducer()

    def handle(self, request: ModelReviewBudgetCommand) -> ModelReviewBudgetDecision:
        # Revalidate typed event boundaries too: model_copy is not a validation operation.
        request = ModelReviewBudgetCommand.model_validate(request.model_dump())
        maximum = (
            self._pricing.quote(request.quote.model_copy(update={"mode": request.mode}))
            if request.quote is not None and request.operation == "reserve"
            else None
        )

        def reduce(state: ModelReviewBudgetState) -> ModelReviewBudgetReduction:
            # Settlement uses the persisted mode, never a caller's claimed discount.
            reservation = state.reservations.get(str(request.call_id))
            measured = (
                self._pricing.measured_cost(
                    request.usage, batch=reservation.mode == "batch"
                )
                if request.usage is not None and reservation is not None
                else None
            )
            # Stamp the authority clock inside the transaction, after acquiring the lock.
            return self._reducer.handle(
                ModelReviewBudgetTransition(
                    state=state,
                    command=request,
                    policy=self._policy,
                    as_of=self._clock(),
                    worst_case_usd=maximum,
                    measured_usd=measured,
                )
            )

        return self._store.transact(reduce).decision
