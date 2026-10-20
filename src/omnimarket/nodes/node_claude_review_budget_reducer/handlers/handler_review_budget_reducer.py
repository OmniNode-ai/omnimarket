# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure reservation transitions; only the persistence effect issues admission grants."""

from __future__ import annotations

import hashlib
from decimal import Decimal

from omnimarket.models.claude_review_budget.claude_review_budget_math import (
    cycle_end,
    cycle_start,
)
from omnimarket.models.claude_review_budget.model_review_budget_admission import (
    ModelReviewBudgetCommand,
    ModelReviewBudgetDecision,
    ModelReviewBudgetReduction,
    ModelReviewBudgetReservation,
    ModelReviewBudgetTransition,
)


def _fingerprint(command: ModelReviewBudgetCommand) -> str:
    identity = command.model_dump_json(
        include={
            "call_id",
            "api_key_ref",
            "model_id",
            "persona_id",
            "policy_version",
            "quote",
            "mode",
        }
    )
    return hashlib.sha256(identity.encode()).hexdigest()


class HandlerClaudeReviewBudgetReducer:
    """Deterministic state reduction. No clock, store, network or key values."""

    def handle(
        self, request: ModelReviewBudgetTransition
    ) -> ModelReviewBudgetReduction:
        command, policy = request.command, request.policy
        state = request.state.model_copy(deep=True)
        call_id = str(command.call_id)
        existing = state.reservations.get(call_id)
        ref = command.api_key_ref or (
            existing.api_key_ref if existing is not None else ""
        )

        def result(admitted: bool, reason: str) -> ModelReviewBudgetReduction:
            outstanding = {key: Decimal(0) for key in policy.key_limits}
            for reservation in state.reservations.values():
                if reservation.status in {"reserved", "started"}:
                    outstanding[reservation.api_key_ref] = (
                        outstanding.get(reservation.api_key_ref, Decimal(0))
                        + reservation.maximum_usd
                    )
            committed = dict(state.settled_usd)
            for key, amount in outstanding.items():
                committed[key] = committed.get(key, Decimal(0)) + amount
            return ModelReviewBudgetReduction(
                state=state,
                decision=ModelReviewBudgetDecision(
                    admitted=admitted,
                    reason=reason,
                    call_id=command.call_id,
                    key_committed_usd=committed.get(ref, Decimal(0)),
                    aggregate_committed_usd=state.aggregate_settled_usd
                    + sum(outstanding.values(), Decimal(0)),
                    reserved_usd=sum(outstanding.values(), Decimal(0)),
                    policy_version=policy.version,
                ),
            )

        if state.last_as_of is not None and request.as_of < state.last_as_of:
            return result(False, "stale_time")
        started = cycle_start(policy.cycle_anchor_at, request.as_of, 1)
        if state.cycle_started_at is None or started > state.cycle_started_at:
            # Unresolved charges cross cycles conservatively; they never expire on a timer.
            state = state.model_copy(
                update={
                    "cycle_started_at": started,
                    "aggregate_settled_usd": Decimal(0),
                }
            )
        for key, key_limit in policy.key_limits.items():
            anchor = key_limit.cycle_anchor_at or policy.cycle_anchor_at
            if anchor > request.as_of:
                if key == ref:
                    return result(False, "key_cycle_not_started")
                continue
            key_started = cycle_start(anchor, request.as_of, 1)
            previous_start = state.key_cycle_started_at.get(key)
            if previous_start is None or key_started > previous_start:
                state.key_cycle_started_at[key] = key_started
                state.settled_usd[key] = Decimal(0)
        state = state.model_copy(update={"last_as_of": request.as_of})
        fingerprint = _fingerprint(command)
        existing = state.reservations.get(call_id)
        if existing is not None:
            if command.operation == "reserve" and existing.fingerprint != fingerprint:
                return result(False, "reservation_conflict")
            if (
                command.api_key_ref is not None
                and command.api_key_ref != existing.api_key_ref
            ) or (
                command.model_id is not None and command.model_id != existing.model_id
            ):
                return result(False, "reservation_conflict")

        if command.operation == "reserve":
            if command.policy_version != policy.version:
                return result(False, "policy_mismatch")
            if command.persona_id is None or command.model_id is None:
                return result(False, "invalid_reservation")
            persona_policy = policy.personas.get(command.persona_id)
            if persona_policy is None:
                return result(False, "unknown_persona")
            limit = policy.key_limits.get(ref)
            if limit is None:
                return result(False, "unknown_key")
            if existing is not None:
                return result(
                    existing.status in {"reserved", "started"}, existing.status
                )
            cost = request.worst_case_usd
            if cost is None:
                return result(False, "model_not_priced")
            totals = result(False, "readback").decision
            if (
                totals.key_committed_usd + cost
                > limit.allowance_usd - limit.reserve_usd
            ):
                return result(False, "key_limit")
            if totals.aggregate_committed_usd + cost > policy.aggregate_limit_usd:
                return result(False, "aggregate_limit")
            elapsed = Decimal(str((request.as_of - started).total_seconds()))
            duration = Decimal(
                str(
                    (
                        cycle_end(policy.cycle_anchor_at, request.as_of, 1) - started
                    ).total_seconds()
                )
            )
            paced_limit = (
                policy.aggregate_limit_usd * elapsed / duration
                + policy.pacing_reserve_usd
            )
            if (
                persona_policy.enforce_pacing
                and totals.aggregate_committed_usd + cost > paced_limit
            ):
                return result(False, "paced_budget")
            state.reservations[call_id] = ModelReviewBudgetReservation(
                fingerprint=fingerprint,
                api_key_ref=ref,
                model_id=command.model_id,
                mode=command.mode,
                maximum_usd=cost,
                status="reserved",
            )
            return result(True, "reserved")

        if command.operation == "reconcile":
            evidence = command.reconcile_evidence
            amount = command.reconciled_spend_usd
            if not evidence or amount is None or ref not in policy.key_limits:
                return result(False, "invalid_reconciliation")
            digest = hashlib.sha256(command.model_dump_json().encode()).hexdigest()
            previous = state.reconciliations.get(evidence)
            if previous is not None:
                return result(
                    False,
                    "reconciled" if previous == digest else "reconciliation_conflict",
                )
            for resolved_id in command.resolved_call_ids:
                resolved = state.reservations.get(str(resolved_id))
                if resolved is None or resolved.api_key_ref != ref:
                    return result(False, "invalid_reconciliation")
            old_spend = state.settled_usd.get(ref, Decimal(0))
            aggregate = command.aggregate_reconciled_spend_usd
            if aggregate is None:
                # Organization balance windows can differ from the aggregate month.
                # Refuse to infer a monthly aggregate from a differently anchored bill.
                if state.key_cycle_started_at.get(ref) != state.cycle_started_at:
                    return result(False, "aggregate_reconciliation_required")
                aggregate = state.aggregate_settled_usd + amount - old_spend
            if aggregate < 0:
                return result(False, "invalid_reconciliation")
            state = state.model_copy(update={"aggregate_settled_usd": aggregate})
            state.settled_usd[ref] = amount
            for resolved_id in command.resolved_call_ids:
                item = state.reservations[str(resolved_id)]
                if item.status in {"reserved", "started"}:
                    state.reservations[str(resolved_id)] = item.model_copy(
                        update={"status": "reconciled"}
                    )
            state.reconciliations[evidence] = digest
            return result(False, "reconciled")

        if existing is None:
            return result(False, "unknown_reservation")
        if command.operation == "started":
            if existing.status != "reserved":
                return result(False, "already_" + existing.status)
            state.reservations[call_id] = existing.model_copy(
                update={"status": "started"}
            )
            return result(True, "started")
        if command.operation == "settle":
            if existing.status == "settled":
                return result(
                    False,
                    "settled"
                    if existing.usage == command.usage
                    else "settlement_conflict",
                )
            if existing.status == "reconciled":
                return result(False, "reconciled")
            if existing.status != "started":
                return result(False, "not_started")
            if request.measured_usd is None or command.usage is None:
                return result(False, "model_not_priced")
            if (
                command.usage.api_key_ref != existing.api_key_ref
                or command.usage.model_id != existing.model_id
            ):
                return result(False, "usage_conflict")
            state = state.model_copy(
                update={
                    "aggregate_settled_usd": state.aggregate_settled_usd
                    + request.measured_usd,
                }
            )
            state.settled_usd[ref] = (
                state.settled_usd.get(ref, Decimal(0)) + request.measured_usd
            )
            state.reservations[call_id] = existing.model_copy(
                update={
                    "status": "settled",
                    "usage": command.usage,
                }
            )
            return result(False, "settled")
        if command.operation == "release":
            if existing.status == "started" and not command.zero_charge_evidence:
                return result(False, "charge_unknown")
            if existing.status in {"reserved", "started"}:
                state.reservations[call_id] = existing.model_copy(
                    update={"status": "released"}
                )
            return result(False, state.reservations[call_id].status)
        return result(False, "invalid_operation")
