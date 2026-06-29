# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR lifecycle state reducer handler — pure FSM reducer.

The reducer is the ONLY authority for phase transitions.
Pure function: delta(state, event) -> (new_state, intents[]).

Entry flags control transition availability:
  - dry_run: all transitions allowed, no side-effect intents emitted
  - inventory_only: stops after INVENTORYING (no FIXING or MERGING)
  - fix_only: only TRIAGED -> FIXING allowed (skips MERGING)

Repo-health fold functions (OMN-13585):
  - fold_repo_health_classified: folds repo-health-classified.v1 events into
    ModelPrLifecycleState.repo_health only; all PR-lane fields are untouched.
  - fold_repo_health_repair_emitted: folds repo-health-repair-emitted.v1 events
    into ModelPrLifecycleState.repo_health only; all PR-lane fields are untouched.

Related:
    - OMN-8086: Create pr_lifecycle_state_reducer Node
    - OMN-8070: PR Lifecycle Domain epic
    - OMN-13585: RH-3: Extend pr_lifecycle reducer state with additive repo_health sub-record
    - OMN-13321: F5: emit durable per-PR ledger projection every sweep iteration
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from omnimarket.nodes.node_pr_lifecycle_state_reducer.models.model_pr_lifecycle_event import (
    EnumPrLifecycleEventTrigger,
    EnumPrLifecyclePhase,
    ModelPrLifecycleEvent,
)
from omnimarket.nodes.node_pr_lifecycle_state_reducer.models.model_pr_lifecycle_intent import (
    EnumPrLifecycleIntentType,
    ModelPrLifecycleIntent,
)
from omnimarket.nodes.node_pr_lifecycle_state_reducer.models.model_pr_lifecycle_state import (
    ModelPrLifecycleEntryFlags,
    ModelPrLifecycleState,
)
from omnimarket.projection.pr_ledger_projection import (
    PR_LEDGER_PROJECTION_CONFLICT_KEY,
    PR_LEDGER_PROJECTION_TABLE,
    build_ledger_rows,
)
from omnimarket.projection.protocol_database import ProtocolProjectionDatabaseSync

logger = logging.getLogger(__name__)

# Handler type/category as Literals
HandlerType = Literal["NODE_HANDLER"]
HandlerCategory = Literal["COMPUTE"]

# ---------------------------------------------------------------------------
# DAG dependency ordering (OMN-8205)
# ---------------------------------------------------------------------------

# Canonical dependency order (repo slug -> priority tier, lower = earlier).
# Add new repos here with the appropriate tier number.
_REPO_DEPENDENCY_TIERS: dict[str, int] = {
    "omnibase_compat": 0,
    "omnibase_spi": 1,
    "omnibase_core": 2,
    "omnibase_infra": 3,
    "omnimarket": 4,
    "omniclaude": 5,
    "omniintelligence": 6,
    "omnimemory": 7,
    "omninode_infra": 8,
    "onex_change_control": 9,
    "omnidash": 10,
    "omniweb": 11,
    # Unknown repos: tier 99 (merge last) — add new repos here
}

_UNKNOWN_TIER = 99
_GREEN_CATEGORY = "green"


def _repo_slug(repo: str) -> str:
    """Extract the bare repo slug from an owner/repo string."""
    return repo.split("/")[-1]


def _apply_dag_ordering(prs: list[Any]) -> list[Any]:
    """Sort PRs into dependency-safe merge order.

    Rules:
    - Sort by (tier, green_first) where GREEN PRs within a tier sort before non-green.
    - Stable sort: same-tier same-status PRs preserve original order.
    - Unknown repos: tier 99, merge last.

    Args:
        prs: List of TriageRecord-like objects with .repo and .category attributes.

    Returns:
        New list sorted in dependency-safe order.
    """

    def _sort_key(pr: Any) -> tuple[int, int]:
        tier = _REPO_DEPENDENCY_TIERS.get(_repo_slug(pr.repo), _UNKNOWN_TIER)
        # GREEN sorts first (0), non-green sorts second (1)
        is_non_green = 0 if getattr(pr, "category", "") == _GREEN_CATEGORY else 1
        return (tier, is_non_green)

    return sorted(prs, key=_sort_key)


# ---------------------------------------------------------------------------
# Valid FSM transitions: (from_phase, trigger) -> to_phase
_TRANSITIONS: dict[
    tuple[EnumPrLifecyclePhase, EnumPrLifecycleEventTrigger],
    EnumPrLifecyclePhase,
] = {
    (
        EnumPrLifecyclePhase.IDLE,
        EnumPrLifecycleEventTrigger.START_RECEIVED,
    ): EnumPrLifecyclePhase.INVENTORYING,
    (
        EnumPrLifecyclePhase.INVENTORYING,
        EnumPrLifecycleEventTrigger.INVENTORY_COMPLETE,
    ): EnumPrLifecyclePhase.TRIAGED,
    (
        EnumPrLifecyclePhase.INVENTORYING,
        EnumPrLifecycleEventTrigger.ERROR,
    ): EnumPrLifecyclePhase.FAILED,
    (
        EnumPrLifecyclePhase.TRIAGED,
        EnumPrLifecycleEventTrigger.REBASE_PENDING,
    ): EnumPrLifecyclePhase.REBASING,
    (
        EnumPrLifecyclePhase.TRIAGED,
        EnumPrLifecycleEventTrigger.FIXES_PENDING,
    ): EnumPrLifecyclePhase.FIXING,
    (
        EnumPrLifecyclePhase.TRIAGED,
        EnumPrLifecycleEventTrigger.NO_FIXES_NEEDED,
    ): EnumPrLifecyclePhase.MERGING,
    (
        EnumPrLifecyclePhase.TRIAGED,
        EnumPrLifecycleEventTrigger.ERROR,
    ): EnumPrLifecyclePhase.FAILED,
    (
        EnumPrLifecyclePhase.REBASING,
        EnumPrLifecycleEventTrigger.REBASE_COMPLETE,
    ): EnumPrLifecyclePhase.MERGING,
    (
        EnumPrLifecyclePhase.REBASING,
        EnumPrLifecycleEventTrigger.ERROR,
    ): EnumPrLifecyclePhase.FAILED,
    (
        EnumPrLifecyclePhase.FIXING,
        EnumPrLifecycleEventTrigger.FIXES_COMPLETE,
    ): EnumPrLifecyclePhase.MERGING,
    (
        EnumPrLifecyclePhase.FIXING,
        EnumPrLifecycleEventTrigger.ERROR,
    ): EnumPrLifecyclePhase.FAILED,
    (
        EnumPrLifecyclePhase.MERGING,
        EnumPrLifecycleEventTrigger.MERGE_COMPLETE,
    ): EnumPrLifecyclePhase.COMPLETE,
    (
        EnumPrLifecyclePhase.MERGING,
        EnumPrLifecycleEventTrigger.ERROR,
    ): EnumPrLifecyclePhase.FAILED,
}

# Map to_phase -> intent type emitted on transition (for non-dry-run paths)
_PHASE_INTENTS: dict[EnumPrLifecyclePhase, EnumPrLifecycleIntentType] = {
    EnumPrLifecyclePhase.INVENTORYING: EnumPrLifecycleIntentType.START_INVENTORY,
    EnumPrLifecyclePhase.REBASING: EnumPrLifecycleIntentType.START_REBASE,
    EnumPrLifecyclePhase.FIXING: EnumPrLifecycleIntentType.START_FIX,
    EnumPrLifecyclePhase.MERGING: EnumPrLifecycleIntentType.START_MERGE,
    EnumPrLifecyclePhase.COMPLETE: EnumPrLifecycleIntentType.SWEEP_COMPLETE,
    EnumPrLifecyclePhase.FAILED: EnumPrLifecycleIntentType.SWEEP_FAILED,
}

# Terminal phases reject all events
_TERMINAL_PHASES = frozenset(
    {EnumPrLifecyclePhase.COMPLETE, EnumPrLifecyclePhase.FAILED}
)


def _is_transition_allowed(
    from_phase: EnumPrLifecyclePhase,
    to_phase: EnumPrLifecyclePhase,
    flags: ModelPrLifecycleEntryFlags,
) -> bool:
    """Check if a transition is allowed given the entry flags.

    Args:
        from_phase: Current FSM phase.
        to_phase: Target FSM phase.
        flags: Entry flags controlling transition availability.

    Returns:
        True if the transition is allowed.
    """
    # Error transitions always allowed (can always fail)
    if to_phase == EnumPrLifecyclePhase.FAILED:
        return True

    # inventory_only: stop after inventorying — only IDLE->INVENTORYING and INVENTORYING->TRIAGED allowed
    if flags.inventory_only:
        allowed = {
            (EnumPrLifecyclePhase.IDLE, EnumPrLifecyclePhase.INVENTORYING),
            (EnumPrLifecyclePhase.INVENTORYING, EnumPrLifecyclePhase.TRIAGED),
        }
        return (from_phase, to_phase) in allowed

    # fix_only: only TRIAGED->FIXING allowed (no MERGING)
    if flags.fix_only:
        blocked = {
            (EnumPrLifecyclePhase.TRIAGED, EnumPrLifecyclePhase.MERGING),
            (EnumPrLifecyclePhase.FIXING, EnumPrLifecyclePhase.MERGING),
        }
        return (from_phase, to_phase) not in blocked

    return True


# ---------------------------------------------------------------------------
# Repo-health fold functions (OMN-13585)
#
# These are pure functions that fold repo-health domain events into the
# ModelPrLifecycleState.repo_health sub-record ONLY.  The PR-lane FSM fields
# (phase, prs_*, started_at, etc.) are never touched by these folds.
# ---------------------------------------------------------------------------


#: Topic suffixes that route to repo-health fold functions instead of the FSM delta.
#: Full subscribed topics are declared in contract.yaml event_bus.subscribe_topics.
_REPO_HEALTH_CLASSIFIED_TOPIC_SUFFIX = "repo-health-classified.v1"
_REPO_HEALTH_REPAIR_EMITTED_TOPIC_SUFFIX = "repo-health-repair-emitted.v1"

#: Classification values and their target counter field names.
_CLASSIFICATION_FIELD: dict[str, str] = {
    "pr_scoped": "pr_scoped_count",
    "repo_baseline": "repo_baseline_count",
    "external_dependency": "external_dependency_count",
    "unknown": "unknown_count",
}


def fold_repo_health_classified(
    state: ModelPrLifecycleState,
    *,
    classification: str,
    ticket_ref: str | None,
) -> ModelPrLifecycleState:
    """Fold a repo-health-classified event into the repo_health sub-record.

    Only ModelPrLifecycleState.repo_health is updated; all PR-lane FSM fields
    are byte-identical in the returned state.

    Args:
        state: Current PR lifecycle state.
        classification: Classification label from the event.
            One of: 'pr_scoped', 'repo_baseline', 'external_dependency', 'unknown'.
            Unknown labels increment unknown_count.
        ticket_ref: Optional ticket reference string (unused here; reserved for
            future sub-classification linking).

    Returns:
        New ModelPrLifecycleState with updated repo_health only.
    """
    rh = state.repo_health
    counter_field = _CLASSIFICATION_FIELD.get(classification, "unknown_count")
    current_counter: int = getattr(rh, counter_field)
    rh_update: dict[str, object] = {
        "classified_count": rh.classified_count + 1,
        counter_field: current_counter + 1,
    }
    new_rh = rh.model_copy(update=rh_update)
    return state.model_copy(update={"repo_health": new_rh})


def fold_repo_health_repair_emitted(
    state: ModelPrLifecycleState,
    *,
    ticket_ref: str | None,
) -> ModelPrLifecycleState:
    """Fold a repo-health-repair-emitted event into the repo_health sub-record.

    Only ModelPrLifecycleState.repo_health is updated; all PR-lane FSM fields
    are byte-identical in the returned state.

    Duplicate ticket_refs are deduplicated in repair_task_refs.

    Args:
        state: Current PR lifecycle state.
        ticket_ref: Ticket reference string for the emitted repair task,
            or None if no ticket was generated.

    Returns:
        New ModelPrLifecycleState with updated repo_health only.
    """
    rh = state.repo_health
    if ticket_ref is not None:
        # Deduplicate: preserve insertion order via dict.fromkeys
        new_refs: tuple[str, ...] = tuple(
            dict.fromkeys((*rh.repair_task_refs, ticket_ref))
        )
    else:
        new_refs = rh.repair_task_refs
    new_rh = rh.model_copy(
        update={
            "repair_tasks_emitted": rh.repair_tasks_emitted + 1,
            "repair_task_refs": new_refs,
        }
    )
    return state.model_copy(update={"repo_health": new_rh})


class HandlerPrLifecycleStateReducer:
    """Pure reducer: delta(state, event) -> (new_state, intents).

    Entry flags (dry_run, inventory_only, fix_only) control which transitions
    are enabled. No side effects are produced — only state and intent computation.
    """

    @property
    def handler_type(self) -> HandlerType:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> HandlerCategory:
        return "COMPUTE"

    def handle_dict(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """RuntimeLocal handler protocol shim.

        Dispatches to the appropriate fold function based on the event_topic key,
        or delegates to delta() for standard PR-lifecycle FSM events.

        Repo-health events (OMN-13585):
          - onex.evt.omnimarket.repo-health-classified.v1 → fold_repo_health_classified
          - onex.evt.omnimarket.repo-health-repair-emitted.v1 → fold_repo_health_repair_emitted

        All other events → delta() (FSM state transition).
        """
        event_topic: str = input_data.get("event_topic", "")
        state_data = input_data.get("state", {})
        event_data = input_data.get("event", {})
        state = ModelPrLifecycleState(**state_data)

        if event_topic.endswith(_REPO_HEALTH_CLASSIFIED_TOPIC_SUFFIX):
            new_state = fold_repo_health_classified(
                state,
                classification=event_data.get("classification", "unknown"),
                ticket_ref=event_data.get("ticket_ref"),
            )
            return {
                "state": new_state.model_dump(mode="json"),
                "intents": [],
            }

        if event_topic.endswith(_REPO_HEALTH_REPAIR_EMITTED_TOPIC_SUFFIX):
            new_state = fold_repo_health_repair_emitted(
                state,
                ticket_ref=event_data.get("ticket_ref"),
            )
            return {
                "state": new_state.model_dump(mode="json"),
                "intents": [],
            }

        # Default: FSM delta path
        event = ModelPrLifecycleEvent(**event_data)
        new_state, intents = self.delta(state, event)
        return {
            "state": new_state.model_dump(mode="json"),
            "intents": [i.model_dump(mode="json") for i in intents],
        }

    async def handle(
        self,
        *args: Any,
        correlation_id: UUID | None = None,
        classified: tuple[Any, ...] | None = None,
        dry_run: bool = False,
        inventory_only: bool = False,
        fix_only: bool = False,
        merge_only: bool = False,
        projection_db: ProtocolProjectionDatabaseSync | None = None,
        sweep_id: str | None = None,
        iteration: int = 0,
        found_at: datetime | None = None,
        **_kwargs: Any,
    ) -> Any:
        """Unified entry point: dispatches to orchestrator or RuntimeLocal shim.

        When called with orchestrator kwargs (correlation_id, classified), acts as
        ProtocolStateReducerHandler — classifying triage records into intents and
        returning a ReducerResult.

        When called with a positional dict (RuntimeLocal shim path), delegates to
        handle_dict() for FSM delta computation.

        Entry flag semantics (orchestrator path):
          - inventory_only: skip all PRs (no merge, no fix)
          - fix_only: only FIX intents, no MERGE
          - merge_only: only MERGE intents, no FIX
          - dry_run: compute and return intents, orchestrator will not execute them

        Per-iteration ledger projection (OMN-13321 / F5):
          - projection_db + sweep_id: emit one durable ledger row per PR for
            this iteration into pr_lifecycle_ledger_entries. Both must be
            supplied together; supplying one without the other fails fast.
          - iteration: monotonic per-sweep index (part of the conflict key,
            so two consecutive iterations produce distinct rows).
          - found_at: observation timestamp for the row; defaults to now().
        """
        # RuntimeLocal shim: positional dict argument
        if args:
            return self.handle_dict(args[0])

        # Orchestrator path: keyword args matching ProtocolStateReducerHandler
        from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
            EnumPrCategory,
            EnumReducerIntent,
            ReducerIntent,
            ReducerResult,
        )

        classified_prs: tuple[Any, ...] = classified if classified is not None else ()
        intents: list[ReducerIntent] = []

        for pr in classified_prs:
            if inventory_only:
                intent = EnumReducerIntent.SKIP
            elif pr.category == EnumPrCategory.GREEN:
                intent = EnumReducerIntent.SKIP if fix_only else EnumReducerIntent.MERGE
            elif pr.category == EnumPrCategory.OCC_DEPENDENCY:
                intent = EnumReducerIntent.SKIP
            elif pr.category in (
                EnumPrCategory.RED,
                EnumPrCategory.CONFLICTED,
                EnumPrCategory.NEEDS_REVIEW,
            ):
                intent = EnumReducerIntent.SKIP if merge_only else EnumReducerIntent.FIX
            else:
                intent = EnumReducerIntent.SKIP

            intents.append(
                ReducerIntent(
                    pr_number=pr.pr_number,
                    repo=pr.repo,
                    intent=intent,
                    ticket_ids=getattr(pr, "ticket_ids", ()),
                    reason=pr.block_reason,
                )
            )

        merge_count = sum(1 for i in intents if i.intent == EnumReducerIntent.MERGE)
        fix_count = sum(1 for i in intents if i.intent == EnumReducerIntent.FIX)
        skip_count = sum(1 for i in intents if i.intent == EnumReducerIntent.SKIP)

        logger.info(
            "[STATE-REDUCER] correlation_id=%s classified=%d merge=%d fix=%d skip=%d "
            "dry_run=%s inventory_only=%s fix_only=%s merge_only=%s",
            correlation_id,
            len(classified_prs),
            merge_count,
            fix_count,
            skip_count,
            dry_run,
            inventory_only,
            fix_only,
            merge_only,
        )

        # OMN-13321 / F5: emit one durable, user-readable ledger row per PR
        # EVERY iteration (not only at sweep end). The reducer is the named
        # surface; rows land in the control-plane projection database keyed by
        # (sweep_id, repo, pr_number, iteration), so two consecutive
        # iterations both produce rows and neither overwrites the other.
        self._emit_ledger_rows(
            classified=classified_prs,
            intents=intents,
            projection_db=projection_db,
            sweep_id=sweep_id,
            iteration=iteration,
            found_at=found_at,
        )

        return ReducerResult(
            intents=tuple(intents),
            merge_count=merge_count,
            fix_count=fix_count,
            skip_count=skip_count,
        )

    def _emit_ledger_rows(
        self,
        *,
        classified: tuple[Any, ...],
        intents: list[Any],
        projection_db: ProtocolProjectionDatabaseSync | None,
        sweep_id: str | None,
        iteration: int,
        found_at: datetime | None,
    ) -> None:
        """Materialize per-iteration ledger rows into the projection database.

        OMN-13321 / F5. When no projection_db AND no sweep_id are supplied the
        reducer is the pure classifier (local/test path) and nothing is
        emitted. Supplying one without the other is a wiring bug and fails
        fast -- a half-configured durable surface silently dropping rows is
        exactly the OMN-12569 failure mode this ticket hardens against.

        ``found_at`` defaults to wall-clock now when the durable path is
        active; ``next_check_at`` is derived from the contract freshness SLA
        inside ``build_ledger_rows``.
        """
        if projection_db is None and sweep_id is None:
            return
        if projection_db is None or sweep_id is None:
            raise ValueError(
                "per-iteration ledger projection requires BOTH projection_db "
                "and sweep_id; got projection_db="
                f"{projection_db is not None}, sweep_id={sweep_id!r}"
            )
        observed_at = found_at if found_at is not None else datetime.now(tz=UTC)
        rows = build_ledger_rows(
            sweep_id=sweep_id,
            iteration=iteration,
            found_at=observed_at,
            classified=classified,
            intents=intents,
        )
        for row in rows:
            projection_db.upsert(
                PR_LEDGER_PROJECTION_TABLE,
                PR_LEDGER_PROJECTION_CONFLICT_KEY,
                row.to_row(),
            )
        logger.info(
            "[STATE-REDUCER] ledger projection: sweep_id=%s iteration=%d rows=%d",
            sweep_id,
            iteration,
            len(rows),
        )

    def delta(
        self,
        state: ModelPrLifecycleState,
        event: ModelPrLifecycleEvent,
    ) -> tuple[ModelPrLifecycleState, list[ModelPrLifecycleIntent]]:
        """Compute the next state and intents from current state + event.

        Args:
            state: Current FSM state.
            event: Incoming event.

        Returns:
            Tuple of (new_state, intents_to_emit).
        """
        # Reject correlation_id mismatch
        if event.correlation_id != state.correlation_id:
            logger.warning(
                "Rejecting event: correlation_id mismatch (event=%s, state=%s)",
                event.correlation_id,
                state.correlation_id,
            )
            return state, []

        # Reject out-of-order: event source_phase must match current phase
        if event.source_phase != state.phase:
            logger.warning(
                "Rejecting out-of-order event: source_phase=%s but current phase=%s",
                event.source_phase.value,
                state.phase.value,
            )
            return state, []

        # Terminal states reject all events
        if state.phase in _TERMINAL_PHASES:
            logger.warning(
                "Rejecting event: already in terminal phase %s", state.phase.value
            )
            return state, []

        # Look up the transition
        transition_key = (state.phase, event.trigger)
        to_phase = _TRANSITIONS.get(transition_key)
        if to_phase is None:
            logger.error(
                "No transition defined for phase=%s trigger=%s",
                state.phase.value,
                event.trigger.value,
            )
            return state, []

        # Check entry flag constraints
        if not _is_transition_allowed(state.phase, to_phase, state.entry_flags):
            logger.info(
                "Transition %s -> %s blocked by entry flags (inventory_only=%s, fix_only=%s)",
                state.phase.value,
                to_phase.value,
                state.entry_flags.inventory_only,
                state.entry_flags.fix_only,
            )
            # Transition to COMPLETE when blocked by flags (sweep is done for this mode)
            to_phase = EnumPrLifecyclePhase.COMPLETE

        # Build state update
        update: dict[str, object] = {
            "phase": to_phase,
            "last_phase_at": event.timestamp,
        }

        if to_phase == EnumPrLifecyclePhase.FAILED:
            update["error_message"] = event.error_message
        else:
            update["error_message"] = None

        # Capture phase-specific metrics
        if event.prs_inventoried > 0:
            update["prs_inventoried"] = event.prs_inventoried
        if event.prs_blocked > 0:
            update["prs_blocked"] = event.prs_blocked
        if event.prs_fixed > 0:
            update["prs_fixed"] = event.prs_fixed
        if event.prs_merged > 0:
            update["prs_merged"] = event.prs_merged

        # Set started_at on first transition from IDLE
        if state.phase == EnumPrLifecyclePhase.IDLE:
            update["started_at"] = event.timestamp

        # Compute total prs_processed on completion
        if to_phase == EnumPrLifecyclePhase.COMPLETE:
            update["prs_processed"] = (
                state.prs_inventoried + event.prs_inventoried
                if event.prs_inventoried > state.prs_inventoried
                else state.prs_inventoried
            )

        new_state = state.model_copy(update=update)

        # Emit intents — dry_run suppresses side-effect intents
        intents: list[ModelPrLifecycleIntent] = []
        intent_type = _PHASE_INTENTS.get(to_phase)
        if intent_type is not None:
            # dry_run: suppress all intents except SWEEP_COMPLETE and SWEEP_FAILED
            if state.entry_flags.dry_run and intent_type not in (
                EnumPrLifecycleIntentType.SWEEP_COMPLETE,
                EnumPrLifecycleIntentType.SWEEP_FAILED,
            ):
                logger.info(
                    "dry_run=True: suppressing intent %s for phase %s",
                    intent_type.value,
                    to_phase.value,
                )
            else:
                intents.append(
                    ModelPrLifecycleIntent(
                        intent_type=intent_type,
                        correlation_id=state.correlation_id,
                        from_phase=to_phase,
                    )
                )

        logger.info(
            "Transition: %s -> %s (correlation=%s, trigger=%s)",
            state.phase.value,
            to_phase.value,
            state.correlation_id,
            event.trigger.value,
        )

        return new_state, intents
