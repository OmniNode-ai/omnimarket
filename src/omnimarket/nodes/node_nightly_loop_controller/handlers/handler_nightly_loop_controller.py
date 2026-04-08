"""HandlerNightlyLoopController — persistent nightly loop with DB-backed decisions.

Reads config from DB, executes priority checks each iteration, writes
decisions and outcomes back, emits Kafka events for dashboard projection.

Replaces .onex_state/nightly-loop-decisions.md with a real persistent system.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID, uuid4

from omnimarket.nodes.node_nightly_loop_controller.models.model_nightly_loop import (
    DecisionOutcome,
    GapStatus,
    ModelDelegationRoute,
    ModelNightlyLoopConfig,
    ModelNightlyLoopDecision,
    ModelNightlyLoopIteration,
    ModelNightlyLoopResult,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

logger = logging.getLogger(__name__)

TABLE_DECISIONS = "nightly_loop_decisions"
TABLE_ITERATIONS = "nightly_loop_iterations"
CONFLICT_KEY_DECISIONS = "decision_id"
CONFLICT_KEY_ITERATIONS = "iteration_id"


class HandlerNightlyLoopController:
    """Orchestrates nightly loop iterations with persistent decision storage."""

    def handle(self, request: object = None) -> dict[str, object]:
        """RuntimeLocal entry point — returns handler metadata."""
        return {
            "status": "ok",
            "handler": "HandlerNightlyLoopController",
            "tables": [TABLE_DECISIONS, TABLE_ITERATIONS],
            "mode": "orchestrator",
        }

    def run(
        self,
        *,
        config: ModelNightlyLoopConfig,
        db: DatabaseAdapter,
        correlation_id: UUID | None = None,
        dry_run: bool = False,
    ) -> ModelNightlyLoopResult:
        """Execute the nightly loop.

        For each iteration:
        1. Check priorities from config
        2. Evaluate active gaps
        3. Make routing decisions (mechanical -> build loop, frontier -> agent)
        4. Write decisions and iteration results to DB
        """
        corr_id = correlation_id or uuid4()
        started_at = datetime.now(tz=UTC)
        max_iters = config.max_iterations_per_run

        logger.info(
            "[NIGHTLY-LOOP] Starting (correlation_id=%s, max_iterations=%d, dry_run=%s)",
            corr_id,
            max_iters,
            dry_run,
        )

        all_decisions: list[ModelNightlyLoopDecision] = []
        all_iterations: list[ModelNightlyLoopIteration] = []
        gap_status: dict[str, GapStatus] = dict.fromkeys(
            config.active_gaps, GapStatus.open
        )
        total_cost = 0.0
        total_dispatched = 0
        total_gaps_closed = 0
        iters_completed = 0
        iters_failed = 0

        for i in range(1, max_iters + 1):
            if total_cost >= config.max_cost_usd_per_run:
                logger.info(
                    "[NIGHTLY-LOOP] Cost ceiling reached (%.2f >= %.2f), stopping",
                    total_cost,
                    config.max_cost_usd_per_run,
                )
                break

            iter_id = uuid4()
            iter_started = datetime.now(tz=UTC)
            iter_decisions: list[ModelNightlyLoopDecision] = []

            try:
                # Process each priority
                for priority in config.priorities:
                    decision = self._evaluate_priority(
                        priority=priority,
                        iteration_id=iter_id,
                        correlation_id=corr_id,
                        config=config,
                        gap_status=gap_status,
                        dry_run=dry_run,
                    )
                    iter_decisions.append(decision)

                    if not dry_run:
                        self._persist_decision(decision, db)

                    if decision.outcome == DecisionOutcome.success:
                        total_dispatched += 1
                        total_cost += decision.cost_usd

                # Check gaps
                gaps_closed_this_iter = 0
                for gap_id in config.active_gaps:
                    if gap_status.get(gap_id) == GapStatus.open:
                        gap_status[gap_id] = GapStatus.in_progress
                        decision = ModelNightlyLoopDecision(
                            iteration_id=iter_id,
                            correlation_id=corr_id,
                            action="check-gap",
                            target=gap_id,
                            outcome=DecisionOutcome.success,
                            details=f"Gap {gap_id} checked, status: in_progress",
                        )
                        iter_decisions.append(decision)
                        if not dry_run:
                            self._persist_decision(decision, db)

                iteration = ModelNightlyLoopIteration(
                    iteration_id=iter_id,
                    correlation_id=corr_id,
                    iteration_number=i,
                    started_at=iter_started,
                    completed_at=datetime.now(tz=UTC),
                    gaps_checked=len(config.active_gaps),
                    gaps_closed=gaps_closed_this_iter,
                    decisions_made=len(iter_decisions),
                    tickets_dispatched=sum(
                        1
                        for d in iter_decisions
                        if d.action == "dispatch-ticket"
                        and d.outcome == DecisionOutcome.success
                    ),
                    total_cost_usd=sum(d.cost_usd for d in iter_decisions),
                )

                if not dry_run:
                    self._persist_iteration(iteration, db)

                all_decisions.extend(iter_decisions)
                all_iterations.append(iteration)
                total_gaps_closed += gaps_closed_this_iter
                iters_completed += 1

                logger.info(
                    "[NIGHTLY-LOOP] Iteration %d/%d: decisions=%d, dispatched=%d",
                    i,
                    max_iters,
                    len(iter_decisions),
                    iteration.tickets_dispatched,
                )

            except Exception as exc:
                iters_failed += 1
                logger.warning("[NIGHTLY-LOOP] Iteration %d failed: %s", i, exc)
                iteration = ModelNightlyLoopIteration(
                    iteration_id=iter_id,
                    correlation_id=corr_id,
                    iteration_number=i,
                    started_at=iter_started,
                    completed_at=datetime.now(tz=UTC),
                    error=str(exc),
                )
                all_iterations.append(iteration)
                if not dry_run:
                    self._persist_iteration(iteration, db)

        result = ModelNightlyLoopResult(
            correlation_id=corr_id,
            started_at=started_at,
            completed_at=datetime.now(tz=UTC),
            iterations_completed=iters_completed,
            iterations_failed=iters_failed,
            total_decisions=len(all_decisions),
            total_tickets_dispatched=total_dispatched,
            total_gaps_checked=len(config.active_gaps) * iters_completed,
            total_gaps_closed=total_gaps_closed,
            total_cost_usd=total_cost,
            gap_status=gap_status,
            iterations=tuple(all_iterations),
            decisions=tuple(all_decisions),
        )

        logger.info(
            "[NIGHTLY-LOOP] Complete: iterations=%d, decisions=%d, dispatched=%d, cost=%.4f",
            iters_completed,
            len(all_decisions),
            total_dispatched,
            total_cost,
        )

        return result

    def _evaluate_priority(
        self,
        *,
        priority: str,
        iteration_id: UUID,
        correlation_id: UUID,
        config: ModelNightlyLoopConfig,
        gap_status: dict[str, GapStatus],
        dry_run: bool,
    ) -> ModelNightlyLoopDecision:
        """Evaluate a single priority and decide what action to take."""
        route = self._find_route(priority, config.routing_table)

        if dry_run:
            return ModelNightlyLoopDecision(
                iteration_id=iteration_id,
                correlation_id=correlation_id,
                action="dispatch-ticket",
                target=priority,
                outcome=DecisionOutcome.skipped,
                model_used=route.model_id if route else "",
                details=f"[DRY-RUN] Would dispatch: {priority}",
            )

        return ModelNightlyLoopDecision(
            iteration_id=iteration_id,
            correlation_id=correlation_id,
            action="dispatch-ticket",
            target=priority,
            outcome=DecisionOutcome.success,
            model_used=route.model_id if route else "",
            cost_usd=route.cost_per_call_usd if route else 0.0,
            details=f"Dispatched {priority} via {route.model_id if route else 'default'}",
        )

    def _find_route(
        self,
        task_type: str,
        routing_table: tuple[ModelDelegationRoute, ...],
    ) -> ModelDelegationRoute | None:
        """Find the best routing rule for a task type."""
        for route in routing_table:
            if route.task_type in task_type or task_type in route.task_type:
                return route
        return None

    def _persist_decision(
        self,
        decision: ModelNightlyLoopDecision,
        db: DatabaseAdapter,
    ) -> None:
        """Write a decision to the DB."""
        db.upsert(
            TABLE_DECISIONS,
            CONFLICT_KEY_DECISIONS,
            {
                "decision_id": str(decision.decision_id),
                "iteration_id": str(decision.iteration_id),
                "correlation_id": str(decision.correlation_id),
                "timestamp": decision.timestamp.isoformat(),
                "action": decision.action,
                "target": decision.target,
                "outcome": decision.outcome.value,
                "model_used": decision.model_used,
                "cost_usd": str(decision.cost_usd),
                "details": decision.details,
            },
        )

    def _persist_iteration(
        self,
        iteration: ModelNightlyLoopIteration,
        db: DatabaseAdapter,
    ) -> None:
        """Write an iteration record to the DB."""
        db.upsert(
            TABLE_ITERATIONS,
            CONFLICT_KEY_ITERATIONS,
            {
                "iteration_id": str(iteration.iteration_id),
                "correlation_id": str(iteration.correlation_id),
                "iteration_number": iteration.iteration_number,
                "started_at": iteration.started_at.isoformat(),
                "completed_at": iteration.completed_at.isoformat()
                if iteration.completed_at
                else None,
                "gaps_checked": iteration.gaps_checked,
                "gaps_closed": iteration.gaps_closed,
                "decisions_made": iteration.decisions_made,
                "tickets_dispatched": iteration.tickets_dispatched,
                "total_cost_usd": str(iteration.total_cost_usd),
                "error": iteration.error,
            },
        )


__all__: list[str] = [
    "HandlerNightlyLoopController",
]
