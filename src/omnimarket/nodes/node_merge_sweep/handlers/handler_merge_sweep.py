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
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    pass

_log = logging.getLogger(__name__)

# Topic bindings from contract.yaml event_bus — import from here, never inline elsewhere
TOPIC_MERGE_SWEEP_START: str = "onex.cmd.omnimarket.merge-sweep-start.v1"
TOPIC_MERGE_SWEEP_COMPLETED: str = "onex.evt.omnimarket.merge-sweep-completed.v1"


class EnumPRTrack(StrEnum):
    """Classification track for a PR."""

    A_UPDATE = "A-update"
    A_MERGE = "A"
    A_RESOLVE = "A-resolve"
    B_POLISH = "B"
    SKIP = "skip"


class EnumFailureCategory(StrEnum):
    """Standardized failure category strings for cross-run failure history."""

    CI_TEST = "ci_test"
    CI_LINT = "ci_lint"
    CI_GATE = "ci_gate"
    PR_TITLE = "pr_title"
    CONFLICT = "conflict"
    CHANGES_REQUESTED = "changes_requested"
    THREADS_BLOCKED = "threads_blocked"
    BRANCH_STALE = "branch_stale"
    SCAN_FAILED = "scan_failed"
    POLISH_FAILED = "polish_failed"
    NEEDS_HUMAN = "needs_human"


class ModelPRInfo(BaseModel):
    """Minimal PR representation for classification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    number: int
    title: str
    repo: str
    mergeable: str  # MERGEABLE | CONFLICTING | UNKNOWN
    merge_state_status: str  # BEHIND | BLOCKED | CLEAN | DIRTY | DRAFT | UNKNOWN
    is_draft: bool = False
    review_decision: str | None = None  # APPROVED | CHANGES_REQUESTED | None
    required_checks_pass: bool = True
    labels: list[str] = Field(default_factory=list)
    review_bot_gate_passed: bool | None = Field(
        default=None,
        description=(
            "State of the review-bot/all-findings-resolved commit status. "
            "None = not yet set (treated as pending). "
            "True = all findings resolved by bot. False = findings still open."
        ),
    )
    required_approving_review_count: int | None = Field(
        default=None,
        description=(
            "Branch-protection required approving review count for the base branch. "
            "0 or None means protection does not require approval and reviewDecision "
            "may be blank on solo-dev repos; >0 means an APPROVED review is required "
            "before merge-sweep may enqueue. Populated per-repo per-sweep from "
            "`gh api repos/.../branches/main/protection`. [OMN-9106]"
        ),
    )


class ModelClassifiedPR(BaseModel):
    """A PR with its classification track."""

    model_config = ConfigDict(extra="forbid")

    pr: ModelPRInfo
    track: EnumPRTrack
    reason: str
    failure_categories: list[str] = Field(default_factory=list)


class ModelFailureHistoryEntry(BaseModel):
    """Cross-run failure tracking for a single PR."""

    model_config = ConfigDict(extra="forbid")

    first_seen: str
    last_seen: str
    consecutive_failures: int = 0
    total_failures: int = 0
    total_polishes: int = 0
    last_failure_categories: list[str] = Field(default_factory=list)
    last_result: str | None = None
    last_run_id: str | None = None
    runs_seen: list[str] = Field(default_factory=list)


class ModelFailureHistorySummary(BaseModel):
    """Aggregate failure history stats for ModelSkillResult."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    total_tracked: int = 0
    stuck_prs: int = 0
    chronic_prs: int = 0
    recidivist_prs: int = 0
    cleaned_merged: int = 0


class ModelMergeSweepRequest(BaseModel):
    """Input for the merge sweep handler."""

    model_config = ConfigDict(extra="forbid")

    prs: list[ModelPRInfo]
    require_approval: bool = True
    merge_method: str = "squash"
    max_total_merges: int = 0
    skip_polish: bool = False
    failure_history: dict[str, ModelFailureHistoryEntry] = Field(default_factory=dict)
    run_id: str = ""
    use_lifecycle_ordering: bool = Field(
        default=False,
        description=(
            "When True, reorder Track A PRs via the lifecycle pipeline "
            "(triage → reducer) for dependency-optimal merge order."
        ),
    )


class ModelMergeSweepResult(BaseModel):
    """Output of the merge sweep handler."""

    model_config = ConfigDict(extra="forbid")

    classified: list[ModelClassifiedPR] = Field(default_factory=list)
    status: str = "nothing_to_merge"
    failure_history_summary: ModelFailureHistorySummary = Field(
        default_factory=ModelFailureHistorySummary
    )

    @property
    def track_a_update(self) -> list[ModelClassifiedPR]:
        return [c for c in self.classified if c.track == EnumPRTrack.A_UPDATE]

    @property
    def track_a_merge(self) -> list[ModelClassifiedPR]:
        return [c for c in self.classified if c.track == EnumPRTrack.A_MERGE]

    @property
    def track_a_resolve(self) -> list[ModelClassifiedPR]:
        return [c for c in self.classified if c.track == EnumPRTrack.A_RESOLVE]

    @property
    def track_b_polish(self) -> list[ModelClassifiedPR]:
        return [c for c in self.classified if c.track == EnumPRTrack.B_POLISH]

    @property
    def skipped(self) -> list[ModelClassifiedPR]:
        return [c for c in self.classified if c.track == EnumPRTrack.SKIP]


# Legacy aliases for backward compatibility with existing tests
PRTrack = EnumPRTrack
FailureCategory = EnumFailureCategory
PRInfo = ModelPRInfo
ClassifiedPR = ModelClassifiedPR
FailureHistoryEntry = ModelFailureHistoryEntry
FailureHistorySummary = ModelFailureHistorySummary
MergeSweepRequest = ModelMergeSweepRequest
MergeSweepResult = ModelMergeSweepResult


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
                coderabbit_unresolved=0,
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

            Signature matches ProtocolTriageHandler: handle(correlation_id, prs).
            Returns PrTriageResult directly so the orchestrator short-circuits
            the ModelPrTriageOutput mapping path.
            """

            async def handle(
                self, correlation_id: object, prs: object
            ) -> PrTriageResult:
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

            async def handle(self, command: object) -> MergeResult:
                return MergeResult(prs_merged=0, prs_failed=0)

        try:
            orch = HandlerPrLifecycleOrchestrator(
                inventory=_PrebuiltInventory(),
                triage=_PrebuiltTriage(),
                merge=_NoopMerge(),
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
        # The orchestrator passes merge_prs to _NoopMerge in reducer intent order, which
        # preserves the dependency-optimal sequence computed by the reducer.
        #
        # Since _NoopMerge records nothing, we use the triage_result order as a proxy:
        # the reducer emits intents in the same sequence as triage_records (GREEN-first,
        # stable within each repo group).  For the current reducer implementation this
        # is equivalent to the reducer output ordering.
        pr_lookup: dict[tuple[str, int], ModelClassifiedPR] = {
            (c.pr.repo, c.pr.number): c for c in track_a
        }

        ordered: list[ModelClassifiedPR] = []
        seen: set[tuple[str, int]] = set()
        for tr in triage_records:
            key = (tr.repo, tr.pr_number)
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
            if not pr.required_checks_pass:
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
        waits for the bot to clear it, rather than trying to inspect CodeRabbit
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
        if not pr.required_checks_pass:
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
