"""HandlerPrLifecycleOrchestrator — FSM orchestrator for pr_lifecycle domain.

Wires 5 sub-handlers (inventory, triage, reducer, merge, fix) via FSM-driven
execution. The reducer controls state transitions; the orchestrator dispatches
to the appropriate sub-handler based on reducer intents.

Entry flags control which phases are active:
    - dry_run: no side effects (inventory + triage only)
    - inventory_only: stop after inventory
    - fix_only: skip merge, dispatch fix for non-green PRs
    - merge_only: skip fix, only merge green PRs
    - repos: comma-separated repo filter (empty = all)

FSM: IDLE -> INVENTORYING -> TRIAGING -> [MERGING|FIXING] -> COMPLETE | FAILED

Sub-handler dependencies (injected via protocol DI):
    - ProtocolInventoryHandler     (node_pr_lifecycle_inventory_compute)
    - ProtocolTriageHandler        (node_pr_lifecycle_triage_compute)
    - ProtocolStateReducerHandler  (node_pr_lifecycle_state_reducer)
    - ProtocolMergeHandler         (node_pr_lifecycle_merge_effect)
    - ProtocolFixHandler           (node_pr_lifecycle_fix_effect)

Related:
    - OMN-8087: Create pr_lifecycle_orchestrator Node
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
    EnumReducerIntent,
    FixResult,
    InventoryResult,
    MergeResult,
    ProtocolFixHandler,
    ProtocolInventoryHandler,
    ProtocolMergeHandler,
    ProtocolStateReducerHandler,
    ProtocolTriageHandler,
    PrTriageResult,
    ReducerResult,
)

if TYPE_CHECKING:
    from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
        ProtocolEventBusPublisher,
    )

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Input / output models
# ---------------------------------------------------------------------------


class ModelPrLifecycleStartCommand(BaseModel):
    """Start command for the PR lifecycle orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="Unique sweep run ID.")
    dry_run: bool = Field(default=False)
    inventory_only: bool = Field(default=False)
    fix_only: bool = Field(default=False)
    merge_only: bool = Field(default=False)
    repos: str = Field(
        default="",
        description="Comma-separated repo slugs to filter (empty = all).",
    )
    # Merge-sweep upgrade capabilities (OMN-8197)
    enable_auto_rebase: bool = Field(
        default=True,
        description="Auto-rebase stale branches (BEHIND/UNKNOWN) before merge attempt.",
    )
    use_dag_ordering: bool = Field(
        default=True,
        description="Merge PRs in repo dependency order (omnibase_compat first, omnidash last).",
    )
    enable_trivial_comment_resolution: bool = Field(
        default=True,
        description="Auto-resolve trivial CodeRabbit/bot review threads before merge.",
    )
    enable_admin_merge_fallback: bool = Field(
        default=False,
        description="Admin-merge PRs stuck in queue past threshold (opt-in only).",
    )
    admin_fallback_threshold_minutes: int = Field(
        default=30,
        description="Minutes before a merge-queued PR is considered stuck.",
    )


class ModelPrLifecycleResult(BaseModel):
    """Result returned by the orchestrator after a sweep run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    prs_inventoried: int = Field(default=0, ge=0)
    prs_merged: int = Field(default=0, ge=0)
    prs_fixed: int = Field(default=0, ge=0)
    prs_skipped: int = Field(default=0, ge=0)
    final_state: str = Field(default="COMPLETE")
    error_message: str | None = Field(default=None)


# ---------------------------------------------------------------------------
# FSM state
# ---------------------------------------------------------------------------


class EnumOrchestratorState(StrEnum):
    IDLE = "IDLE"
    INVENTORYING = "INVENTORYING"
    TRIAGING = "TRIAGING"
    MERGING = "MERGING"
    FIXING = "FIXING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


_TERMINAL_STATES = {EnumOrchestratorState.COMPLETE, EnumOrchestratorState.FAILED}


@dataclass
class _SweepState:
    """Mutable sweep state tracked across phases."""

    fsm: EnumOrchestratorState = EnumOrchestratorState.IDLE
    prs_inventoried: int = 0
    prs_merged: int = 0
    prs_fixed: int = 0
    prs_skipped: int = 0
    error_message: str | None = None

    # Inter-phase data
    inventory_result: InventoryResult | None = None
    triage_result: PrTriageResult | None = None
    reducer_result: ReducerResult | None = None


# ---------------------------------------------------------------------------
# Default stub implementations (used when sub-nodes not yet available)
# ---------------------------------------------------------------------------


class _StubInventoryHandler:
    async def handle(
        self,
        *,
        correlation_id: UUID,
        repos: tuple[str, ...] = (),
        dry_run: bool = False,
    ) -> InventoryResult:
        logger.warning("[PR-LIFECYCLE-ORCH] inventory stub called (sub-node not wired)")
        return InventoryResult(prs=(), total_collected=0)


class _StubTriageHandler:
    async def handle(
        self,
        *,
        correlation_id: UUID,
        prs: Any,
    ) -> PrTriageResult:
        logger.warning("[PR-LIFECYCLE-ORCH] triage stub called (sub-node not wired)")
        return PrTriageResult(classified=(), green_count=0, non_green_count=0)


class _StubReducerHandler:
    async def handle(
        self,
        *,
        correlation_id: UUID,
        classified: Any,
        dry_run: bool = False,
        inventory_only: bool = False,
        fix_only: bool = False,
        merge_only: bool = False,
    ) -> ReducerResult:
        logger.warning("[PR-LIFECYCLE-ORCH] reducer stub called (sub-node not wired)")
        return ReducerResult(intents=(), merge_count=0, fix_count=0, skip_count=0)


class _StubMergeHandler:
    async def handle(
        self,
        *,
        correlation_id: UUID,
        prs_to_merge: Any,
        dry_run: bool = False,
    ) -> MergeResult:
        logger.warning("[PR-LIFECYCLE-ORCH] merge stub called (sub-node not wired)")
        return MergeResult(prs_merged=0, prs_failed=0)


class _StubFixHandler:
    async def handle(
        self,
        *,
        correlation_id: UUID,
        prs_to_fix: Any,
        dry_run: bool = False,
    ) -> FixResult:
        logger.warning("[PR-LIFECYCLE-ORCH] fix stub called (sub-node not wired)")
        return FixResult(prs_dispatched=0, prs_skipped=0)


# ---------------------------------------------------------------------------
# Orchestrator handler
# ---------------------------------------------------------------------------


def _load_contract(contract_path: Path | None = None) -> dict[str, Any]:
    _path = contract_path or Path(__file__).parent.parent / "contract.yaml"
    with open(_path) as f:
        data: dict[str, Any] = yaml.safe_load(f)
    return data


class HandlerPrLifecycleOrchestrator:
    """FSM orchestrator composing 5 pr_lifecycle sub-handlers.

    All sub-handler arguments are optional to support zero-arg construction by
    the auto-wiring runtime (``onex run``). When omitted, stub implementations
    are used until the real sub-nodes are available.
    """

    def __init__(
        self,
        *,
        inventory: ProtocolInventoryHandler | None = None,
        triage: ProtocolTriageHandler | None = None,
        reducer: ProtocolStateReducerHandler | None = None,
        merge: ProtocolMergeHandler | None = None,
        fix: ProtocolFixHandler | None = None,
        event_bus: ProtocolEventBusPublisher | None = None,
        contract_path: Path | None = None,
    ) -> None:
        contract = _load_contract(contract_path)
        publish_topics: list[str] = contract.get("event_bus", {}).get(
            "publish_topics", []
        )
        self._topic_phase_transition = next(
            (t for t in publish_topics if "phase-transition" in t), ""
        )
        self._topic_completed = next(
            (t for t in publish_topics if "completed" in t), ""
        )

        self._inventory = inventory
        self._triage = triage
        self._reducer = reducer
        self._merge = merge
        self._fix = fix
        self._event_bus = event_bus

    def _ensure_sub_handlers(self) -> None:
        """Lazy-initialize sub-handlers via import fallback if not injected."""
        if self._inventory is None:
            try:
                from omnimarket.nodes.node_pr_lifecycle_inventory_compute.handlers.handler_pr_lifecycle_inventory import (
                    HandlerPrLifecycleInventory,
                )

                self._inventory = cast(
                    ProtocolInventoryHandler, HandlerPrLifecycleInventory()
                )
            except ImportError:
                self._inventory = _StubInventoryHandler()
        if self._triage is None:
            try:
                from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_pr_lifecycle_triage import (
                    HandlerPrLifecycleTriage,
                )

                self._triage = cast(ProtocolTriageHandler, HandlerPrLifecycleTriage())
            except ImportError:
                self._triage = _StubTriageHandler()
        if self._reducer is None:
            try:
                from omnimarket.nodes.node_pr_lifecycle_state_reducer.handlers.handler_pr_lifecycle_state_reducer import (
                    HandlerPrLifecycleStateReducer,
                )

                self._reducer = cast(
                    ProtocolStateReducerHandler, HandlerPrLifecycleStateReducer()
                )
            except ImportError:
                self._reducer = _StubReducerHandler()
        if self._merge is None:
            try:
                from omnimarket.nodes.node_pr_lifecycle_merge_effect.handlers.handler_pr_lifecycle_merge import (
                    HandlerPrLifecycleMerge,
                )

                self._merge = cast(ProtocolMergeHandler, HandlerPrLifecycleMerge())
            except ImportError:
                self._merge = _StubMergeHandler()
        if self._fix is None:
            try:
                from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
                    HandlerPrLifecycleFix,
                )

                self._fix = cast(ProtocolFixHandler, HandlerPrLifecycleFix())
            except ImportError:
                self._fix = _StubFixHandler()

    async def handle(
        self,
        command: ModelPrLifecycleStartCommand,
    ) -> ModelPrLifecycleResult:
        """Run the PR lifecycle sweep."""
        self._ensure_sub_handlers()

        logger.info(
            "[PR-LIFECYCLE-ORCH] === ENTRY === correlation_id=%s "
            "dry_run=%s inventory_only=%s fix_only=%s merge_only=%s repos=%r",
            command.correlation_id,
            command.dry_run,
            command.inventory_only,
            command.fix_only,
            command.merge_only,
            command.repos,
        )

        state = _SweepState()
        repos_filter = tuple(r.strip() for r in command.repos.split(",") if r.strip())

        try:
            # Phase: INVENTORYING
            state.fsm = EnumOrchestratorState.INVENTORYING
            await self._publish_phase_event(
                "IDLE", "INVENTORYING", command.correlation_id
            )

            assert self._inventory is not None
            inv_result = await self._inventory.handle(
                correlation_id=command.correlation_id,
                repos=repos_filter,
                dry_run=command.dry_run,
            )
            state.inventory_result = inv_result
            state.prs_inventoried = inv_result.total_collected
            logger.info(
                "[PR-LIFECYCLE-ORCH] inventory completed: %d PRs",
                inv_result.total_collected,
            )

            if command.inventory_only:
                state.fsm = EnumOrchestratorState.COMPLETE
                await self._publish_phase_event(
                    "INVENTORYING", "COMPLETE", command.correlation_id
                )
                return self._build_result(state, command.correlation_id)

            # Phase: TRIAGING
            state.fsm = EnumOrchestratorState.TRIAGING
            await self._publish_phase_event(
                "INVENTORYING", "TRIAGING", command.correlation_id
            )

            assert self._triage is not None
            triage_result = await self._triage.handle(
                correlation_id=command.correlation_id,
                prs=inv_result.prs,
            )
            state.triage_result = triage_result
            logger.info(
                "[PR-LIFECYCLE-ORCH] triage completed: %d green, %d non-green",
                triage_result.green_count,
                triage_result.non_green_count,
            )

            # Reducer: compute intents from triage result + flags
            assert self._reducer is not None
            reducer_result = await self._reducer.handle(
                correlation_id=command.correlation_id,
                classified=triage_result.classified,
                dry_run=command.dry_run,
                inventory_only=command.inventory_only,
                fix_only=command.fix_only,
                merge_only=command.merge_only,
            )
            state.reducer_result = reducer_result

            if command.dry_run:
                # dry_run: record intents but do not execute
                state.prs_skipped = len(reducer_result.intents)
                state.fsm = EnumOrchestratorState.COMPLETE
                await self._publish_phase_event(
                    "TRIAGING", "COMPLETE", command.correlation_id
                )
                return self._build_result(state, command.correlation_id)

            # Build per-intent sets
            merge_prs = tuple(
                tr
                for intent in reducer_result.intents
                for tr in triage_result.classified
                if tr.pr_number == intent.pr_number
                and tr.repo == intent.repo
                and intent.intent == EnumReducerIntent.MERGE
            )
            fix_prs = tuple(
                tr
                for intent in reducer_result.intents
                for tr in triage_result.classified
                if tr.pr_number == intent.pr_number
                and tr.repo == intent.repo
                and intent.intent == EnumReducerIntent.FIX
            )
            skip_prs = tuple(
                intent
                for intent in reducer_result.intents
                if intent.intent == EnumReducerIntent.SKIP
            )
            state.prs_skipped = len(skip_prs)

            # Phase: MERGING (skip if fix_only)
            if merge_prs and not command.fix_only:
                state.fsm = EnumOrchestratorState.MERGING
                await self._publish_phase_event(
                    "TRIAGING", "MERGING", command.correlation_id
                )

                assert self._merge is not None
                merge_result = await self._merge.handle(
                    correlation_id=command.correlation_id,
                    prs_to_merge=merge_prs,
                    dry_run=command.dry_run,
                )
                state.prs_merged = merge_result.prs_merged
                logger.info(
                    "[PR-LIFECYCLE-ORCH] merge completed: %d merged, %d failed",
                    merge_result.prs_merged,
                    merge_result.prs_failed,
                )

                if command.merge_only:
                    state.fsm = EnumOrchestratorState.COMPLETE
                    await self._publish_phase_event(
                        "MERGING", "COMPLETE", command.correlation_id
                    )
                    return self._build_result(state, command.correlation_id)

                next_from = "MERGING"
            else:
                next_from = "TRIAGING"

            # Phase: FIXING (skip if merge_only)
            if fix_prs and not command.merge_only:
                state.fsm = EnumOrchestratorState.FIXING
                await self._publish_phase_event(
                    next_from, "FIXING", command.correlation_id
                )

                assert self._fix is not None
                fix_result = await self._fix.handle(
                    correlation_id=command.correlation_id,
                    prs_to_fix=fix_prs,
                    dry_run=command.dry_run,
                )
                state.prs_fixed = fix_result.prs_dispatched
                state.prs_skipped += fix_result.prs_skipped
                logger.info(
                    "[PR-LIFECYCLE-ORCH] fix completed: %d dispatched, %d skipped",
                    fix_result.prs_dispatched,
                    fix_result.prs_skipped,
                )
                next_from = "FIXING"

            state.fsm = EnumOrchestratorState.COMPLETE
            await self._publish_phase_event(
                next_from, "COMPLETE", command.correlation_id
            )

        except Exception as exc:
            logger.exception(
                "[PR-LIFECYCLE-ORCH] failed in phase %s: %s",
                state.fsm.value,
                exc,
            )
            state.error_message = str(exc)
            state.fsm = EnumOrchestratorState.FAILED
            await self._publish_phase_event(
                state.fsm.value, "FAILED", command.correlation_id
            )

        logger.info(
            "[PR-LIFECYCLE-ORCH] === EXIT === state=%s prs_inventoried=%d "
            "prs_merged=%d prs_fixed=%d prs_skipped=%d",
            state.fsm.value,
            state.prs_inventoried,
            state.prs_merged,
            state.prs_fixed,
            state.prs_skipped,
        )
        return self._build_result(state, command.correlation_id)

    def _build_result(
        self, state: _SweepState, correlation_id: UUID
    ) -> ModelPrLifecycleResult:
        return ModelPrLifecycleResult(
            correlation_id=correlation_id,
            prs_inventoried=state.prs_inventoried,
            prs_merged=state.prs_merged,
            prs_fixed=state.prs_fixed,
            prs_skipped=state.prs_skipped,
            final_state=state.fsm.value,
            error_message=state.error_message,
        )

    async def _publish_phase_event(
        self,
        from_state: str,
        to_state: str,
        correlation_id: UUID,
    ) -> None:
        if self._event_bus is None:
            return
        payload = json.dumps(
            {
                "from_phase": from_state.lower(),
                "to_phase": to_state.lower(),
                "correlation_id": str(correlation_id),
            }
        ).encode()
        await self._event_bus.publish(
            topic=self._topic_phase_transition,
            key=None,
            value=payload,
        )


__all__: list[str] = [
    "HandlerPrLifecycleOrchestrator",
    "ModelPrLifecycleResult",
    "ModelPrLifecycleStartCommand",
]
