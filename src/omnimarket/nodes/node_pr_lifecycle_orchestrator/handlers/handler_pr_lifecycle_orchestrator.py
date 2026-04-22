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

import asyncio
import inspect
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
    EnumPrCategory,
    EnumReducerIntent,
    FixResult,
    InventoryResult,
    MergeResult,
    ProtocolFixHandler,
    ProtocolInventoryHandler,
    ProtocolMergeHandler,
    ProtocolStateReducerHandler,
    ProtocolTriageHandler,
    PrRecord,
    PrTriageResult,
    ReducerResult,
    TriageRecord,
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
    run_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9._-]+$",
        description=(
            "Human-readable sweep run identifier used as the result.json "
            "directory name under $ONEX_STATE_DIR/merge-sweep/{run_id}/. "
            "Typically YYYYMMDD-HHMMSS-<random6>. "
            "Restricted to [A-Za-z0-9._-] to prevent path traversal when "
            "interpolated into filesystem paths."
        ),
    )
    dry_run: bool = Field(default=False)
    inventory_only: bool = Field(default=False)
    fix_only: bool = Field(default=False)
    merge_only: bool = Field(default=False)
    repos: str = Field(
        default="",
        description="Comma-separated repo slugs to filter (empty = all).",
    )
    max_parallel_polish: int = Field(
        default=20,
        ge=1,
        description="Maximum concurrent pr-polish agents dispatched during Track B (FIXING phase).",
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
        default=True,
        description=(
            "Admin-merge PRs stuck in queue past threshold. "
            "Default ON; pass --no-admin-merge-fallback (or set False) to disable."
        ),
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
    """Stub matching HandlerPrLifecycleInventory.handle(input_model) signature."""

    def handle(self, input_model: Any) -> Any:
        logger.warning("[PR-LIFECYCLE-ORCH] inventory stub called (sub-node not wired)")
        return InventoryResult(prs=(), total_collected=0)


class _StubTriageHandler:
    """Stub matching HandlerPrLifecycleTriage.handle(correlation_id, prs) signature."""

    async def handle(
        self,
        correlation_id: UUID,
        prs: Any,
    ) -> Any:
        logger.warning("[PR-LIFECYCLE-ORCH] triage stub called (sub-node not wired)")
        return PrTriageResult(classified=(), green_count=0, non_green_count=0)


class _StubReducerHandler:
    """Stub matching HandlerPrLifecycleStateReducer.handle(*args, **kwargs) signature."""

    async def handle(
        self,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        logger.warning("[PR-LIFECYCLE-ORCH] reducer stub called (sub-node not wired)")
        return ReducerResult(intents=(), merge_count=0, fix_count=0, skip_count=0)


class _StubMergeHandler:
    """Stub matching HandlerPrLifecycleMerge.handle(command) signature."""

    async def handle(self, command: Any) -> Any:
        logger.warning("[PR-LIFECYCLE-ORCH] merge stub called (sub-node not wired)")
        return MergeResult(prs_merged=0, prs_failed=0)


class _StubFixHandler:
    """Stub matching HandlerPrLifecycleFix.handle(command) signature."""

    async def handle(self, command: Any) -> Any:
        logger.warning("[PR-LIFECYCLE-ORCH] fix stub called (sub-node not wired)")
        return FixResult(prs_dispatched=0, prs_skipped=0)


# ---------------------------------------------------------------------------
# Model-translation helpers (PrRecord ↔ real sub-handler input models)
# ---------------------------------------------------------------------------

_CI_STATUS_MAP: dict[str, str] = {
    "success": "passing",
    "failure": "failing",
    "pending": "pending",
    "unknown": "unknown",
}

_REVIEW_STATUS_MAP: dict[str, str] = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "changes_requested",
    "REVIEW_REQUIRED": "pending",
    "COMMENT": "pending",
}


def _map_ci_status(pr_state: Any) -> str:
    """Map ModelPrState fields to orchestrator-internal checks_status string."""
    if getattr(pr_state, "ci_passing", None) is True:
        return "success"
    if getattr(pr_state, "ci_passing", None) is False:
        return "failure"
    return "unknown"


def _map_review_status(pr_state: Any) -> str:
    """Map ModelPrState.review_decision to orchestrator-internal review_status."""
    decision: str = getattr(pr_state, "review_decision", "") or ""
    return _REVIEW_STATUS_MAP.get(decision.upper(), "unknown")


def _orch_checks_to_ci_status(checks_status: str) -> str:
    """Convert orchestrator checks_status to triage node's ci_status vocabulary."""
    return _CI_STATUS_MAP.get(checks_status.lower(), "unknown")


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
        self._topic_fixer_dispatch_start = next(
            (t for t in publish_topics if "fixer-dispatch-start" in t), ""
        )

        self._inventory = inventory
        self._triage = triage
        self._reducer = reducer
        self._merge = merge
        self._fix = fix
        self._event_bus = event_bus

    @staticmethod
    def _check_protocol_conformance(
        handler: object,
        protocol_cls: type,
        handler_name: str,
    ) -> None:
        """Verify handler conforms to the expected protocol at registration time.

        ``@runtime_checkable`` ``isinstance()`` only checks for attribute
        presence, not signature, so a drifted ``handle()`` (e.g. keyword-only
        args where the protocol declares positional) would silently pass
        ``isinstance`` and fail at dispatch with ``TypeError``. This method
        adds a parameter-name comparison against the protocol's declared
        ``handle()`` signature to catch that drift early.

        Protocols that use ``*args, **kwargs`` (e.g. ProtocolStateReducerHandler)
        are treated as accepting any signature and are not parameter-name
        checked — only the presence of a callable ``handle`` is required.

        Raises:
            TypeError: if the handler does not conform to the protocol.
        """
        if not isinstance(handler, protocol_cls):
            raise TypeError(
                f"{handler_name} ({type(handler).__name__}) does not conform to "
                f"{protocol_cls.__name__}: missing required 'handle' method"
            )
        handle_fn = getattr(handler, "handle", None)
        if handle_fn is None or not callable(handle_fn):
            raise TypeError(
                f"{handler_name} ({type(handler).__name__}) has no callable 'handle' "
                f"attribute — protocol {protocol_cls.__name__} requires it"
            )
        try:
            handler_sig = inspect.signature(handle_fn)
        except (ValueError, TypeError) as exc:
            raise TypeError(
                f"{handler_name} ({type(handler).__name__}).handle is not inspectable: {exc}"
            ) from exc

        proto_fn = getattr(protocol_cls, "handle", None)
        if proto_fn is None:
            return  # Protocol defines no handle — nothing to compare
        try:
            proto_sig = inspect.signature(proto_fn)
        except (ValueError, TypeError):
            return  # Protocol signature not inspectable — fall back to isinstance only

        # Async/sync parity: registering a sync handler for an async protocol
        # (or vice-versa) fails only at dispatch today. Catch it here.
        proto_is_async = inspect.iscoroutinefunction(proto_fn)
        handler_is_async = inspect.iscoroutinefunction(handle_fn)
        if proto_is_async != handler_is_async:
            raise TypeError(
                f"{handler_name} ({type(handler).__name__}).handle is "
                f"{'async' if handler_is_async else 'sync'} but "
                f"{protocol_cls.__name__}.handle is "
                f"{'async' if proto_is_async else 'sync'}. "
                "Protocol async/sync signature drift — update the handler to match."
            )

        proto_params = [
            p
            for p in proto_sig.parameters.values()
            if p.name != "self" and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
        ]
        proto_has_var = any(
            p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
            for p in proto_sig.parameters.values()
        )
        if proto_has_var and not proto_params:
            # Protocol accepts any signature (e.g. reducer *args/**kwargs) —
            # skip name-level comparison.
            return

        handler_params = [
            p
            for p in handler_sig.parameters.values()
            if p.name != "self" and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
        ]
        handler_param_names = [p.name for p in handler_params]
        proto_param_names = {p.name for p in proto_params}

        for expected_param in proto_params:
            if expected_param.name not in handler_param_names:
                raise TypeError(
                    f"{handler_name} ({type(handler).__name__}).handle signature "
                    f"drifted from {protocol_cls.__name__}: expected parameter "
                    f"{expected_param.name!r} not found in handler signature "
                    f"{handler_param_names}. Protocol requires "
                    f"{[p.name for p in proto_params]}."
                )
            handler_param = handler_sig.parameters[expected_param.name]
            # Reject drift where protocol declares POSITIONAL_OR_KEYWORD but
            # handler has KEYWORD_ONLY (the canonical OMN-9234 drift shape).
            if (
                expected_param.kind
                in (
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.POSITIONAL_ONLY,
                )
                and handler_param.kind == inspect.Parameter.KEYWORD_ONLY
            ):
                raise TypeError(
                    f"{handler_name} ({type(handler).__name__}).handle parameter "
                    f"{expected_param.name!r} is KEYWORD_ONLY but "
                    f"{protocol_cls.__name__} declares it POSITIONAL_OR_KEYWORD. "
                    "Protocol signature drift — update the handler to match."
                )

        # Reject extra required parameters on the handler (params not declared
        # by the protocol and with no default). Such parameters make the
        # handler uncallable via the protocol contract and surface only at
        # dispatch today.
        extra_required = [
            p.name
            for p in handler_params
            if p.name not in proto_param_names and p.default is inspect.Parameter.empty
        ]
        if extra_required:
            raise TypeError(
                f"{handler_name} ({type(handler).__name__}).handle declares "
                f"required parameter(s) {extra_required} not present in "
                f"{protocol_cls.__name__}.handle signature "
                f"{[p.name for p in proto_params]}. Make them optional (add "
                "defaults) or remove them so the handler is callable via the "
                "protocol contract."
            )

    def _ensure_sub_handlers(self) -> None:
        """Lazy-initialize sub-handlers via import fallback if not injected.

        Uses runtime conformance checks (isinstance + inspect.signature) instead
        of cast() so that protocol drift surfaces at instantiation, not dispatch.
        """
        if self._inventory is None:
            try:
                from omnimarket.nodes.node_pr_lifecycle_inventory_compute.handlers.handler_pr_lifecycle_inventory import (
                    HandlerPrLifecycleInventory,
                )

                inv_handler = HandlerPrLifecycleInventory()
                self._check_protocol_conformance(
                    inv_handler, ProtocolInventoryHandler, "inventory"
                )
                self._inventory = inv_handler
            except ImportError:
                self._inventory = _StubInventoryHandler()
        if self._triage is None:
            try:
                from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_pr_lifecycle_triage import (
                    HandlerPrLifecycleTriage,
                )

                triage_handler = HandlerPrLifecycleTriage()
                self._check_protocol_conformance(
                    triage_handler, ProtocolTriageHandler, "triage"
                )
                self._triage = triage_handler
            except ImportError:
                self._triage = _StubTriageHandler()
        if self._reducer is None:
            try:
                from omnimarket.nodes.node_pr_lifecycle_state_reducer.handlers.handler_pr_lifecycle_state_reducer import (
                    HandlerPrLifecycleStateReducer,
                )

                reducer_handler = HandlerPrLifecycleStateReducer()
                self._check_protocol_conformance(
                    reducer_handler, ProtocolStateReducerHandler, "reducer"
                )
                self._reducer = reducer_handler
            except ImportError:
                self._reducer = _StubReducerHandler()
        if self._merge is None:
            try:
                from omnimarket.nodes.node_pr_lifecycle_merge_effect.handlers.handler_pr_lifecycle_merge import (
                    HandlerPrLifecycleMerge,
                )

                merge_handler = HandlerPrLifecycleMerge()
                self._check_protocol_conformance(
                    merge_handler, ProtocolMergeHandler, "merge"
                )
                self._merge = merge_handler
            except ImportError:
                self._merge = _StubMergeHandler()
        if self._fix is None:
            try:
                from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli import (
                    GitHubCliAdapter,
                )
                from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_pr_polish_dispatch import (
                    PrPolishDispatchAdapter,
                )
                from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
                    HandlerPrLifecycleFix,
                )

                fix_handler = HandlerPrLifecycleFix(
                    github_adapter=GitHubCliAdapter(),
                    agent_dispatch_adapter=PrPolishDispatchAdapter(),
                )
                self._check_protocol_conformance(fix_handler, ProtocolFixHandler, "fix")
                self._fix = fix_handler
            except ImportError:
                self._fix = _StubFixHandler()

    async def handle(
        self,
        command: ModelPrLifecycleStartCommand,
    ) -> ModelPrLifecycleResult:
        """Run the PR lifecycle sweep and persist the result for skill polling.

        Writes ``$ONEX_STATE_DIR/merge-sweep/{run_id}/result.json`` on both
        success and failure paths. The merge_sweep skill (v4.0.0+) polls this
        file to determine orchestrator completion.
        """
        try:
            result = await self._run_sweep(command)
        except BaseException as exc:
            # Final safety net — even unexpected errors must produce a result.json
            # so the polling skill can terminate instead of timing out.
            logger.exception(
                "[PR-LIFECYCLE-ORCH] unexpected failure outside FSM: %s", exc
            )
            result = ModelPrLifecycleResult(
                correlation_id=command.correlation_id,
                final_state=EnumOrchestratorState.FAILED.value,
                error_message=str(exc),
            )
            self._write_result_file(command.run_id, result)
            raise
        self._write_result_file(command.run_id, result)
        return result

    async def _run_sweep(
        self,
        command: ModelPrLifecycleStartCommand,
    ) -> ModelPrLifecycleResult:
        """Execute the FSM sweep (caller handles result.json persistence)."""
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
            # Real inventory handler signature: handle(input_model: ModelPrInventoryInput)
            # The orchestrator aggregates across all repos; we call once per repo and
            # merge the results into a single InventoryResult.
            inv_result = await self._call_inventory(
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
            # Real triage handler signature: handle(correlation_id, prs: tuple[ModelPrInventoryItem])
            # Convert PrRecord → ModelPrInventoryItem before calling.
            triage_result = await self._call_triage(
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
                # Real merge handler signature: handle(command: ModelPrMergeCommand)
                # Fan out: one command per PR, aggregate the results.
                merge_result = await self._call_merge_fanout(
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
                await self._publish_fixer_dispatch_start(
                    fix_prs, command.correlation_id
                )

                assert self._fix is not None
                fix_results = await self._dispatch_fix_parallel(
                    fix_prs=fix_prs,
                    correlation_id=command.correlation_id,
                    dry_run=command.dry_run,
                    max_parallel=command.max_parallel_polish,
                    enable_admin_merge_fallback=command.enable_admin_merge_fallback,
                    admin_fallback_threshold_minutes=command.admin_fallback_threshold_minutes,
                )
                state.prs_fixed = sum(r.prs_dispatched for r in fix_results)
                state.prs_skipped += sum(r.prs_skipped for r in fix_results)
                logger.info(
                    "[PR-LIFECYCLE-ORCH] fix completed: %d dispatched, %d skipped",
                    state.prs_fixed,
                    sum(r.prs_skipped for r in fix_results),
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

    def _enumerate_open_pr_numbers(self, repo: str) -> tuple[int, ...]:
        """Enumerate open PR numbers for a single repo via the gh CLI.

        Override in tests (or subclasses) to avoid real network calls.
        Returns an empty tuple on any error. Non-zero ``gh`` exit codes are
        logged with stderr so auth or rate-limit failures are visible rather
        than silently producing zero PRs.
        """
        import subprocess

        try:
            proc = subprocess.run(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    repo,
                    "--state",
                    "open",
                    "--limit",
                    "100",
                    "--json",
                    "number",
                    "--jq",
                    ".[].number",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0:
                logger.warning(
                    "[PR-LIFECYCLE-ORCH] gh pr list failed for repo=%s "
                    "(returncode=%d): %s",
                    repo,
                    proc.returncode,
                    proc.stderr.strip() or "<no stderr>",
                )
                return ()
            return tuple(
                int(n.strip()) for n in proc.stdout.splitlines() if n.strip().isdigit()
            )
        except Exception as exc:
            logger.warning(
                "[PR-LIFECYCLE-ORCH] failed to list PRs for repo=%s: %s",
                repo,
                exc,
            )
            return ()

    def _enumerate_repos(self) -> tuple[str, ...]:
        """Enumerate all org repos via the gh CLI.

        Override in tests (or subclasses) to avoid real network calls.
        Returns an empty tuple on any error. Non-zero ``gh`` exit codes are
        logged with stderr so auth or rate-limit failures are visible rather
        than silently producing zero repos.
        """
        import subprocess

        try:
            proc = subprocess.run(
                [
                    "gh",
                    "repo",
                    "list",
                    "OmniNode-ai",
                    "--limit",
                    "100",
                    "--json",
                    "nameWithOwner",
                    "--jq",
                    ".[].nameWithOwner",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode != 0:
                logger.warning(
                    "[PR-LIFECYCLE-ORCH] gh repo list failed (returncode=%d): %s",
                    proc.returncode,
                    proc.stderr.strip() or "<no stderr>",
                )
                return ()
            return tuple(r.strip() for r in proc.stdout.splitlines() if r.strip())
        except Exception as exc:
            logger.warning("[PR-LIFECYCLE-ORCH] failed to enumerate org repos: %s", exc)
            return ()

    async def _call_inventory(
        self,
        *,
        repos: tuple[str, ...],
        dry_run: bool,
    ) -> InventoryResult:
        """Call the inventory handler with its real input-model signature.

        HandlerPrLifecycleInventory.handle() takes a ModelPrInventoryInput
        with a single ``repo`` + list of PR numbers. For a full-org sweep the
        orchestrator enumerates open PRs per repo (via _enumerate_open_pr_numbers),
        then delegates each repo batch to the inventory handler.  When no repos
        are specified, _enumerate_repos() discovers all org repos first.

        Both enumeration methods are overridable hooks so tests can inject
        synthetic PR data without real gh CLI calls.

        This method wraps that per-repo fan-out and adapts the per-repo
        ModelPrInventoryOutput results into the orchestrator-internal
        InventoryResult (list of PrRecord).

        Short-circuit: if the handler returns an InventoryResult directly
        (i.e. a mock that bypasses ModelPrInventoryInput), that result is
        returned as-is, allowing test mocks to return fixture data without
        needing real PR number enumeration.
        """
        assert self._inventory is not None

        from omnimarket.nodes.node_pr_lifecycle_inventory_compute.models.model_pr_lifecycle_inventory import (
            ModelPrInventoryInput,
        )

        if not repos:
            repos = self._enumerate_repos()

        all_prs: list[PrRecord] = []
        for repo in repos:
            pr_numbers = self._enumerate_open_pr_numbers(repo)
            if not pr_numbers:
                continue

            input_model = ModelPrInventoryInput(repo=repo, pr_numbers=pr_numbers)
            raw = self._inventory.handle(input_model)
            # Short-circuit: test stub returned InventoryResult directly.
            if isinstance(raw, InventoryResult):
                return raw
            # raw is ModelPrInventoryOutput; adapt to PrRecord sequence.
            for pr_state in getattr(raw, "pr_states", ()):
                all_prs.append(
                    PrRecord(
                        pr_number=pr_state.pr_number,
                        repo=pr_state.repo,
                        title=getattr(pr_state, "title", ""),
                        branch=getattr(pr_state, "head_ref", ""),
                        checks_status=_map_ci_status(pr_state),
                        review_status=_map_review_status(pr_state),
                        has_conflicts=getattr(pr_state, "has_conflicts", False),
                        merge_state_status=getattr(
                            pr_state, "merge_state_status", None
                        ),
                    )
                )

        return InventoryResult(prs=tuple(all_prs), total_collected=len(all_prs))

    async def _call_triage(
        self,
        *,
        correlation_id: UUID,
        prs: tuple[PrRecord, ...],
    ) -> PrTriageResult:
        """Call the triage handler with its real signature.

        HandlerPrLifecycleTriage.handle(correlation_id, prs: tuple[ModelPrInventoryItem])
        → ModelPrTriageOutput.

        Adapts PrRecord → ModelPrInventoryItem before the call, then maps
        ModelPrTriageOutput → PrTriageResult.
        """
        assert self._triage is not None

        from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_pr_inventory_item import (
            ModelPrInventoryItem,
        )

        items = tuple(
            ModelPrInventoryItem(
                pr_number=pr.pr_number,
                repo=pr.repo,
                title=pr.title,
                branch=pr.branch,
                ci_status=_orch_checks_to_ci_status(pr.checks_status),
                has_conflicts=pr.has_conflicts,
                approved=(pr.review_status == "approved"),
            )
            for pr in prs
        )

        raw = await self._triage.handle(correlation_id, items)

        # Short-circuit: test stub returned PrTriageResult directly.
        if isinstance(raw, PrTriageResult):
            return raw

        # Map ModelPrTriageOutput → PrTriageResult
        classified: list[TriageRecord] = []
        green_count = 0
        non_green_count = 0
        for result in getattr(raw, "results", ()):
            cat_value: str = getattr(
                getattr(result, "category", None),
                "value",
                str(getattr(result, "category", "unknown")),
            )
            try:
                category = EnumPrCategory(cat_value)
            except ValueError:
                category = EnumPrCategory.UNKNOWN
            classified.append(
                TriageRecord(
                    pr_number=result.pr_number,
                    repo=result.repo,
                    category=category,
                    block_reason=getattr(result, "reason", ""),
                )
            )
            if category == EnumPrCategory.GREEN:
                green_count += 1
            else:
                non_green_count += 1

        return PrTriageResult(
            classified=tuple(classified),
            green_count=green_count,
            non_green_count=non_green_count,
        )

    async def _call_merge_fanout(
        self,
        *,
        correlation_id: UUID,
        prs_to_merge: tuple[TriageRecord, ...],
        dry_run: bool,
    ) -> MergeResult:
        """Fan out merge commands to the merge handler (one command per PR).

        HandlerPrLifecycleMerge.handle(command: ModelPrMergeCommand) → ModelPrMergeResult.
        """
        assert self._merge is not None

        from omnimarket.nodes.node_pr_lifecycle_merge_effect.models.model_merge_command import (
            ModelPrMergeCommand,
        )

        prs_merged = 0
        prs_failed = 0
        for pr in prs_to_merge:
            merge_command = ModelPrMergeCommand(
                correlation_id=correlation_id,
                pr_number=pr.pr_number,
                repo=pr.repo,
                triage_verdict=pr.category.value,
                dry_run=dry_run,
                requested_at=datetime.now(tz=UTC),
            )
            try:
                raw = await self._merge.handle(merge_command)
            except Exception as exc:
                # Per-PR isolation: one transient GitHub/network failure must
                # not abort the whole sweep. Count this PR as failed and move on.
                logger.exception(
                    "[PR-LIFECYCLE-ORCH] merge handler raised for "
                    "correlation_id=%s repo=%s pr=%d: %s",
                    correlation_id,
                    pr.repo,
                    pr.pr_number,
                    exc,
                )
                prs_failed += 1
                continue
            if getattr(raw, "merged", False):
                prs_merged += 1
            else:
                prs_failed += 1

        return MergeResult(prs_merged=prs_merged, prs_failed=prs_failed)

    async def _dispatch_fix_parallel(
        self,
        *,
        fix_prs: tuple[TriageRecord, ...],
        correlation_id: UUID,
        dry_run: bool,
        max_parallel: int,
        enable_admin_merge_fallback: bool,
        admin_fallback_threshold_minutes: int,
    ) -> list[FixResult]:
        """Fan out fix dispatch across all PRs in parallel, bounded by max_parallel.

        Each PR gets its own call to the fix handler so they run concurrently.
        A semaphore caps simultaneous in-flight dispatches to max_parallel.
        ``enable_admin_merge_fallback`` flows through to the fix handler so the
        orchestrator boundary actually controls admin-merge behavior — before
        OMN-9114 this flag was orphaned at the command boundary.
        """
        assert self._fix is not None
        semaphore = asyncio.Semaphore(max_parallel)

        async def _fix_one(pr: TriageRecord) -> FixResult:
            async with semaphore:
                # Real fix handler signature: handle(command: ModelPrLifecycleFixCommand)
                # Construct command from TriageRecord fields.
                from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
                    EnumPrBlockReason,
                    ModelPrLifecycleFixCommand,
                )

                block_reason_str = pr.block_reason or "ci_failure"
                try:
                    block_reason = EnumPrBlockReason(block_reason_str)
                except ValueError:
                    block_reason = EnumPrBlockReason.CI_FAILURE

                fix_command = ModelPrLifecycleFixCommand(
                    correlation_id=correlation_id,
                    pr_number=pr.pr_number,
                    repo=pr.repo,
                    block_reason=block_reason,
                    dry_run=dry_run,
                    requested_at=datetime.now(tz=UTC),
                )
                assert self._fix is not None
                raw = await self._fix.handle(fix_command)
                # Map ModelPrLifecycleFixResult → FixResult aggregate.
                if isinstance(raw, FixResult):
                    return raw
                fix_applied: bool = getattr(raw, "fix_applied", False)
                return FixResult(
                    prs_dispatched=1 if fix_applied else 0,
                    prs_skipped=0 if fix_applied else 1,
                )

        logger.info(
            "[PR-LIFECYCLE-ORCH] dispatching %d fix agents (max_parallel=%d)",
            len(fix_prs),
            max_parallel,
        )
        gathered: list[FixResult | BaseException] = list(
            await asyncio.gather(
                *(_fix_one(pr) for pr in fix_prs), return_exceptions=True
            )
        )
        errors: list[Exception] = [r for r in gathered if isinstance(r, Exception)]
        if errors:
            raise ExceptionGroup("fix dispatch errors", errors)
        return [r for r in gathered if isinstance(r, FixResult)]

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

    def _write_result_file(self, run_id: str, result: ModelPrLifecycleResult) -> None:
        """Persist the orchestrator result as ModelSkillResult-shaped JSON.

        The merge_sweep skill polls ``$ONEX_STATE_DIR/merge-sweep/{run_id}/result.json``.
        A missing ``$ONEX_STATE_DIR`` falls back to ``~/.onex_state`` so that local
        test runs still produce a file.
        """
        state_dir = os.environ.get(
            "ONEX_STATE_DIR", os.path.expanduser("~/.onex_state")
        )
        base = (Path(state_dir) / "merge-sweep").resolve()
        out_dir = (base / run_id).resolve()
        # Defense-in-depth: the model-level regex on run_id already forbids
        # path separators, but if someone bypasses validation we still refuse
        # to escape the merge-sweep root.
        if not out_dir.is_relative_to(base):
            logger.error(
                "[PR-LIFECYCLE-ORCH] refusing to write result.json: run_id "
                "escapes merge-sweep root run_id=%s resolved=%s base=%s",
                run_id,
                out_dir,
                base,
            )
            return
        out_path = out_dir / "result.json"

        is_failure = result.final_state == EnumOrchestratorState.FAILED.value
        payload: dict[str, Any] = {
            "skill_name": "merge-sweep",
            "status": "error" if is_failure else "success",
            "run_id": run_id,
            "correlation_id": str(result.correlation_id),
            "final_state": result.final_state,
            "prs_inventoried": result.prs_inventoried,
            "prs_merged": result.prs_merged,
            "prs_fixed": result.prs_fixed,
            "prs_skipped": result.prs_skipped,
            "error_message": result.error_message,
        }

        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(payload, indent=2))
            logger.info(
                "[PR-LIFECYCLE-ORCH] wrote result.json run_id=%s path=%s",
                run_id,
                out_path,
            )
        except OSError as exc:
            # Best-effort: log but do not mask the sweep result for the caller.
            logger.error(
                "[PR-LIFECYCLE-ORCH] failed to write result.json run_id=%s path=%s: %s",
                run_id,
                out_path,
                exc,
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

    async def _publish_fixer_dispatch_start(
        self,
        fix_prs: tuple[TriageRecord, ...],
        correlation_id: UUID,
    ) -> None:
        """Publish fixer-dispatch-start.v1 for each PR entering FIXING phase.

        Enables node_fixer_dispatcher to route each PR stall to the correct
        fixer node (ci_fix_effect, conflict_hunk_effect, rebase_effect).
        """
        if self._event_bus is None or not self._topic_fixer_dispatch_start:
            return
        for pr in fix_prs:
            payload = json.dumps(
                {
                    "pr_number": pr.pr_number,
                    "repo": pr.repo,
                    "stall_category": pr.block_reason or "unknown",
                    "blocking_reason": pr.block_reason or "",
                    "correlation_id": str(correlation_id),
                }
            ).encode()
            await self._event_bus.publish(
                topic=self._topic_fixer_dispatch_start,
                key=None,
                value=payload,
            )


__all__: list[str] = [
    "HandlerPrLifecycleOrchestrator",
    "ModelPrLifecycleResult",
    "ModelPrLifecycleStartCommand",
]
