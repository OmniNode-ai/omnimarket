# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeMergeSweep — Org-wide PR classification and merge orchestration.

Classifies open PRs into tracks:
- Track A-update: Stale branches needing update before merge
- Track A: Merge-ready PRs for auto-merge
- Track A-resolve: PRs blocked only by unresolved review threads
- Track B: PRs with fixable blocking issues for polish

When ``use_lifecycle_ordering=True`` in the request, Track A PRs are reordered
by the lifecycle pipeline (inventory → triage → reducer) so merges happen in
dependency-optimal order rather than the flat listing order from ``gh pr list``.

ONEX node type: ORCHESTRATOR
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from omnimarket.models.merge_sweep.enum_failure_category import EnumFailureCategory
from omnimarket.models.merge_sweep.enum_pr_track import EnumPRTrack
from omnimarket.models.merge_sweep.model_classified_pr import ModelClassifiedPR
from omnimarket.models.merge_sweep.model_failure_history_entry import (
    ModelFailureHistoryEntry,
)
from omnimarket.models.merge_sweep.model_failure_history_summary import (
    ModelFailureHistorySummary,
)
from omnimarket.models.merge_sweep.model_merge_sweep_request import (
    ModelMergeSweepRequest,
)
from omnimarket.models.merge_sweep.model_merge_sweep_result import (
    ModelMergeSweepResult,
)
from omnimarket.models.merge_sweep.model_pr_info import ModelPRInfo
from omnimarket.nodes.contract_topics import contract_topic

if TYPE_CHECKING:
    pass

_log = logging.getLogger(__name__)

# Topic bindings from contract.yaml event_bus — import from here, never inline elsewhere
_CONTRACT = Path(__file__).resolve().parent.parent / "contract.yaml"
TOPIC_MERGE_SWEEP_START: str = contract_topic(
    _CONTRACT, "subscribe_topics", "merge-sweep-start.v1"
)
TOPIC_MERGE_SWEEP_COMPLETED: str = contract_topic(
    _CONTRACT, "publish_topics", "merge-sweep-completed.v1"
)


class NodeMergeSweep:
    """Classify PRs into merge/polish/skip tracks."""

    # Escalation thresholds
    STUCK_THRESHOLD = 3
    CHRONIC_THRESHOLD = 5
    RECIDIVIST_POLISH_THRESHOLD = 3

    def handle(self, request: ModelMergeSweepRequest) -> ModelMergeSweepResult:
        """Classify all PRs and return the result."""
        classified: list[ModelClassifiedPR] = []

        for pr in request.prs:
            track, reason, categories = self._classify_pr(pr, request.require_approval)
            classified.append(
                ModelClassifiedPR(
                    pr=pr, track=track, reason=reason, failure_categories=categories
                )
            )

        # Apply max_total_merges cap to Track A
        if request.max_total_merges > 0:
            merge_count = 0
            for c in classified:
                if c.track == EnumPRTrack.A_MERGE:
                    merge_count += 1
                    if merge_count > request.max_total_merges:
                        c.track = EnumPRTrack.SKIP
                        c.reason = "Exceeds max_total_merges cap"

        # Remove Track B if skip_polish
        if request.skip_polish:
            for c in classified:
                if c.track == EnumPRTrack.B_POLISH:
                    c.track = EnumPRTrack.SKIP
                    c.reason = "Polish skipped (--skip-polish)"

        # Apply failure history escalation to Track B
        for c in classified:
            if c.track == EnumPRTrack.B_POLISH:
                skip, skip_reason = self._should_skip_polish(
                    request.failure_history, f"{c.pr.repo}#{c.pr.number}"
                )
                if skip:
                    c.track = EnumPRTrack.SKIP
                    c.reason = skip_reason or "Skipped by failure history"

        # Reorder Track A using lifecycle pipeline for dependency-optimal merge order
        if request.use_lifecycle_ordering:
            track_a = [c for c in classified if c.track == EnumPRTrack.A_MERGE]
            rest = [c for c in classified if c.track != EnumPRTrack.A_MERGE]
            if track_a:
                ordered_track_a = self._run_lifecycle_ordering(track_a)
                classified = ordered_track_a + rest

        has_actionable = any(
            c.track
            in (
                EnumPRTrack.A_UPDATE,
                EnumPRTrack.A_MERGE,
                EnumPRTrack.A_RESOLVE,
                EnumPRTrack.B_POLISH,
            )
            for c in classified
        )
        status = "queued" if has_actionable else "nothing_to_merge"

        # Compute failure history summary
        summary = self._compute_failure_summary(request.failure_history)

        return ModelMergeSweepResult(
            classified=classified, status=status, failure_history_summary=summary
        )

    def _run_lifecycle_ordering(
        self, track_a: list[ModelClassifiedPR]
    ) -> list[ModelClassifiedPR]:
        """Run ``_lifecycle_ordered_track_a`` safely from synchronous context.

        ``asyncio.run()`` raises if called from a running event loop (e.g.
        inside pytest-asyncio tests).  This method runs the coroutine in a
        dedicated thread with its own event loop so it is safe to call from
        both sync and async callers.
        """
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, self._lifecycle_ordered_track_a(track_a))
            try:
                return future.result(timeout=60)
            except Exception as exc:
                _log.warning(
                    "[merge-sweep] lifecycle ordering thread failed (%s); flat order",
                    exc,
                )
                return track_a

    async def _lifecycle_ordered_track_a(
        self, track_a: list[ModelClassifiedPR]
    ) -> list[ModelClassifiedPR]:
        """Reorder Track A PRs via the lifecycle triage → orchestrator pipeline.

        Converts Track A PRs to inventory items, runs them through the full
        pr_lifecycle_orchestrator (inventory_only=False, merge_only=True) to
        obtain a reducer-ordered MERGE intent list.  PRs are returned in
        reducer-assigned merge order; any PR not emitted by the orchestrator
        is appended at the end (safe fall-through).

        Falls back to the original flat order if any lifecycle node is
        unavailable (ImportError) or raises an unexpected exception.
        """
        try:
            from uuid import uuid4

            from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.handler_pr_lifecycle_orchestrator import (
                HandlerPrLifecycleOrchestrator,
                ModelPrLifecycleStartCommand,
            )
            from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
                EnumPrCategory,
                InventoryResult,
                PrRecord,
                PrTriageResult,
                TriageRecord,
            )
        except ImportError as exc:
            _log.warning(
                "[merge-sweep] lifecycle ordering unavailable (import: %s); flat order",
                exc,
            )
            return track_a

        correlation_id = uuid4()

        # Build PrRecord inventory from Track A PRs (all are green by definition).
        # OMN-9106: treat "approval gate cleared" (APPROVED, or protection
        # doesn't require approval) as "approved" for the reducer so solo-dev
        # repos don't fall into the pending branch.
        def _review_status(pr: ModelPRInfo) -> str:
            if pr.review_decision == "CHANGES_REQUESTED":
                return "changes_requested"
            if pr.review_decision == "APPROVED":
                return "approved"
            if pr.required_approving_review_count in (0, None):
                return "approved"
            return "pending"

        pr_records = tuple(
            PrRecord(
                pr_number=c.pr.number,
                repo=c.pr.repo,
                title=c.pr.title,
                branch="",
                checks_status="success",
                review_status=_review_status(c.pr),
                has_conflicts=False,
            )
            for c in track_a
        )

        # Build pre-classified triage records (all GREEN — they passed _is_merge_ready)
        triage_records = tuple(
            TriageRecord(
                pr_number=c.pr.number,
                repo=c.pr.repo,
                category=EnumPrCategory.GREEN,
                block_reason="",
            )
            for c in track_a
        )

        # Inject pre-built inventory and triage into the orchestrator via stub adapters
        # so it skips the network calls and goes straight to reducer → intent generation.
        # Prebuilt adapters — signatures match real sub-handler protocols (OMN-9234).
        class _PrebuiltInventory:
            """Returns pre-collected inventory without network calls.

            Signature matches ProtocolInventoryHandler: handle(input_model).
            Returns InventoryResult directly so the orchestrator short-circuits
            the pr_states mapping path.
            """

            def handle(self, input_model: object) -> InventoryResult:
                return InventoryResult(prs=pr_records, total_collected=len(pr_records))

        class _PrebuiltTriage:
            """Returns pre-classified triage without network calls.

            Signature matches ProtocolTriageHandler: handle(request).
            Returns PrTriageResult directly so the orchestrator short-circuits
            the ModelPrTriageOutput mapping path.
            """

            async def handle(self, request: object) -> PrTriageResult:
                green_count = len(triage_records)
                return PrTriageResult(
                    classified=triage_records,
                    green_count=green_count,
                    non_green_count=0,
                )

        # Inject a no-op merge handler so the orchestrator computes reducer intents
        # without calling GitHub.  dry_run=False so the reducer emits MERGE intents
        # (dry_run=True suppresses them); the no-op merge handler absorbs the calls.
        from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
            MergeResult,
        )

        class _NoopMerge:
            """No-op merge handler that absorbs MERGE intents without calling GitHub.

            Signature matches ProtocolMergeHandler: handle(command).
            Returns a MagicMock-compatible object with merged=False so
            _call_merge_fanout increments prs_failed but doesn't raise.
            """

            def __init__(self) -> None:
                self.merge_prs: list[tuple[str, int]] = []

            async def handle(self, command: object) -> MergeResult:
                repo = getattr(command, "repo", "")
                pr_number = getattr(command, "pr_number", 0)
                self.merge_prs.append((repo, pr_number))
                return MergeResult(prs_merged=0, prs_failed=0)

        from typing import cast

        from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
            ProtocolEventBusPublisher,
        )

        from omnimarket.nodes.node_merge_sweep_compute.container.container_inmemory import (
            bind_event_bus,
        )

        noop_merge = _NoopMerge()
        try:
            _bus = bind_event_bus()
            await _bus.start()
            orch = HandlerPrLifecycleOrchestrator(
                inventory=_PrebuiltInventory(),
                triage=_PrebuiltTriage(),
                merge=noop_merge,
                event_bus=cast(ProtocolEventBusPublisher, _bus),
            )
            # merge_only=True, dry_run=False: reducer emits MERGE intents in priority order;
            # _NoopMerge absorbs the calls without touching GitHub.
            result = await orch.handle(
                ModelPrLifecycleStartCommand(
                    correlation_id=correlation_id,
                    run_id=f"merge-sweep-inproc-{correlation_id}",
                    merge_only=True,
                    dry_run=False,
                )
            )
        except Exception as exc:
            _log.warning(
                "[merge-sweep] lifecycle orchestrator failed (%s); flat order", exc
            )
            return track_a

        # Rebuild the ordered Track A list by following the reducer's MERGE intent order.
        # The orchestrator passes merge commands to _NoopMerge in reducer intent order,
        # preserving the dependency-optimal sequence without touching GitHub.
        pr_lookup: dict[tuple[str, int], ModelClassifiedPR] = {
            (c.pr.repo, c.pr.number): c for c in track_a
        }

        ordered: list[ModelClassifiedPR] = []
        seen: set[tuple[str, int]] = set()
        for key in noop_merge.merge_prs:
            if key in pr_lookup and key not in seen:
                ordered.append(pr_lookup[key])
                seen.add(key)

        # Safety: append any stragglers not in the triage output
        for c in track_a:
            key = (c.pr.repo, c.pr.number)
            if key not in seen:
                ordered.append(c)

        _log.info(
            "[merge-sweep] lifecycle ordering complete: %d Track A PRs ordered "
            "via pr_lifecycle_orchestrator (final_state=%s)",
            len(track_a),
            result.final_state,
        )
        return ordered

    def _classify_pr(
        self, pr: ModelPRInfo, require_approval: bool
    ) -> tuple[EnumPRTrack, str, list[str]]:
        """Classify a single PR. First match wins. Returns (track, reason, categories)."""
        if pr.is_draft:
            return EnumPRTrack.SKIP, "Draft PR", []

        # Track A-update: stale branches or unknown mergeable state
        if self._needs_branch_update(pr):
            return (
                EnumPRTrack.A_UPDATE,
                f"Branch stale ({pr.merge_state_status})",
                [EnumFailureCategory.BRANCH_STALE.value],
            )

        # Track A: merge-ready
        if self._is_merge_ready(pr, require_approval):
            return EnumPRTrack.A_MERGE, "Merge-ready", []

        # Track A-resolve: BLOCKED by unresolved threads only
        if self._needs_thread_resolution(pr):
            return (
                EnumPRTrack.A_RESOLVE,
                "Blocked by unresolved review threads",
                [EnumFailureCategory.THREADS_BLOCKED.value],
            )

        # Track B: fixable blocking issues
        if self._needs_polish(pr, require_approval):
            categories: list[str] = []
            reason_parts: list[str] = []
            if pr.mergeable == "CONFLICTING":
                reason_parts.append("conflicts")
                categories.append(EnumFailureCategory.CONFLICT.value)
            if self._checks_failed(pr):
                reason_parts.append("CI failing")
                categories.append(EnumFailureCategory.CI_TEST.value)
            if require_approval and pr.review_decision == "CHANGES_REQUESTED":
                reason_parts.append("changes requested")
                categories.append(EnumFailureCategory.CHANGES_REQUESTED.value)
            return (
                EnumPRTrack.B_POLISH,
                "Needs polish: " + ", ".join(reason_parts),
                categories,
            )

        return EnumPRTrack.SKIP, "No actionable state", []

    def _checks_failed(self, pr: ModelPRInfo) -> bool:
        """Return True only for terminal required-check failures."""
        return pr.required_checks_failed

    def _needs_branch_update(self, pr: ModelPRInfo) -> bool:
        if pr.mergeable == "MERGEABLE":
            return pr.merge_state_status.upper() in ("BEHIND", "UNKNOWN")
        if pr.mergeable == "UNKNOWN":
            return True
        return False

    def _is_merge_ready(self, pr: ModelPRInfo, require_approval: bool) -> bool:
        if pr.mergeable != "MERGEABLE":
            return False
        # OMN-8492: review-bot gate explicitly failed — not merge-ready
        if pr.review_bot_gate_passed is False:
            return False
        if pr.merge_state_status.upper() == "BLOCKED":
            return False  # BLOCKED PRs may need thread resolution
        if not pr.required_checks_pass:
            return False
        if require_approval:
            # OMN-9106: clear if APPROVED, or if branch protection does not require
            # approval (solo-dev repos) regardless of reviewDecision being ""/None.
            # CHANGES_REQUESTED always blocks.
            if pr.review_decision == "CHANGES_REQUESTED":
                return False
            if pr.review_decision == "APPROVED":
                return True
            return pr.required_approving_review_count in (0, None)
        return True

    def _needs_thread_resolution(self, pr: ModelPRInfo) -> bool:
        """Blocked by review-bot gate or unresolved conversation threads.

        OMN-8492: merge-sweep now treats review-bot/all-findings-resolved as
        the single authoritative gate. A PR with review_bot_gate_passed=False
        (or None with BLOCKED status) goes to Track A-resolve so merge-sweep
        waits for the bot to clear it, rather than trying to inspect review
        threads directly.
        """
        if pr.mergeable != "MERGEABLE":
            return False
        # Explicit gate failure from the review bot status check
        if pr.review_bot_gate_passed is False:
            return True
        # Legacy path: MERGEABLE + BLOCKED + all other checks green means
        # the only blocker is required_conversation_resolution. Gate not yet set.
        if pr.merge_state_status.upper() != "BLOCKED":
            return False
        if not pr.required_checks_pass:
            return False
        return True

    def _needs_polish(self, pr: ModelPRInfo, require_approval: bool) -> bool:
        if pr.mergeable == "UNKNOWN":
            return False
        if self._is_merge_ready(pr, require_approval):
            return False
        if pr.mergeable == "CONFLICTING":
            return True
        if self._checks_failed(pr):
            return True
        if require_approval and pr.review_decision == "CHANGES_REQUESTED":
            return True
        return False

    def _should_skip_polish(
        self, history: dict[str, ModelFailureHistoryEntry], pr_key: str
    ) -> tuple[bool, str | None]:
        """Check if a PR should skip polish based on failure history."""
        entry = history.get(pr_key)
        if not entry:
            return False, None
        if entry.consecutive_failures >= self.CHRONIC_THRESHOLD:
            return (
                True,
                f"CHRONIC: {entry.consecutive_failures} consecutive failures — skipping polish",
            )
        if (
            entry.total_polishes >= self.RECIDIVIST_POLISH_THRESHOLD
            and entry.consecutive_failures > 0
        ):
            return (
                True,
                f"RECIDIVIST: polished {entry.total_polishes}x, still failing — skipping polish",
            )
        return False, None

    def _compute_failure_summary(
        self, history: dict[str, ModelFailureHistoryEntry]
    ) -> ModelFailureHistorySummary:
        """Compute aggregate failure history stats."""
        stuck = 0
        chronic = 0
        recidivist = 0
        for entry in history.values():
            if entry.consecutive_failures >= self.CHRONIC_THRESHOLD:
                chronic += 1
            elif entry.consecutive_failures >= self.STUCK_THRESHOLD:
                stuck += 1
            if (
                entry.total_polishes >= self.RECIDIVIST_POLISH_THRESHOLD
                and entry.consecutive_failures > 0
            ):
                recidivist += 1
        return ModelFailureHistorySummary(
            total_tracked=len(history),
            stuck_prs=stuck,
            chronic_prs=chronic,
            recidivist_prs=recidivist,
        )
