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
    - OMN-8390: Wire --verify command fields through VERIFYING FSM state
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.events.repo_health import EnumFailureOrigin
from omnimarket.nodes.node_pr_arm_gate_compute.models.model_arm_candidate import (
    ModelArmCandidate,
)
from omnimarket.nodes.node_pr_arm_gate_compute.models.model_arm_gate_decision import (
    EnumArmDecision,
)
from omnimarket.nodes.node_pr_arm_gate_compute.models.model_arm_gate_policy import (
    EnumArmActionMode,
    ModelArmGatePolicy,
)
from omnimarket.nodes.node_pr_arm_gate_compute.models.model_arm_gate_request import (
    ModelArmGateRequest,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.occ_stamp_readback import (
    ProtocolOccStampReadback,
    _UnverifiedOccStampReadback,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.protocols.protocol_sub_handlers import (
    EnumPrCategory,
    EnumReducerIntent,
    FixResult,
    InventoryResult,
    MergeResult,
    OccDependencyEdge,
    ProtocolArmGateHandler,
    ProtocolFixHandler,
    ProtocolInventoryHandler,
    ProtocolMergeHandler,
    ProtocolPruneHandler,
    ProtocolStateReducerHandler,
    ProtocolTriageHandler,
    PrRecord,
    PrTriageResult,
    PruneResult,
    ReducerResult,
    TriageRecord,
)
from omnimarket.nodes.node_pr_lifecycle_orchestrator.verify_target_mapping import (
    EnumVerificationOutcome,
    EnumVerificationTarget,
    map_changed_files_to_target,
    probe_runtime_health,
)
from omnimarket.nodes.pr_ledger_native import (
    EnumOrchestratorAction,
    EnumPrLedgerConclusion,
    EnumPrLedgerEventKind,
    EnumPrLifecyclePhase,
    InMemoryPrLedgerStore,
    ModelPrLedger,
    ModelPrLedgerSourceEvent,
    ModelPrLifecyclePhaseTransition,
    ProtocolPrLedgerStore,
    apply_pr_ledger_event,
    record_phase_transition,
)
from omnimarket.projection.pr_ledger_projection import (
    PR_LEDGER_PROJECTION_TABLE,
)
from omnimarket.projection.protocol_database import ProtocolProjectionDatabaseSync

if TYPE_CHECKING:
    from omnibase_core.protocols.event_bus.protocol_event_bus_publisher import (
        ProtocolEventBusPublisher,
    )

logger = logging.getLogger(__name__)

# OMN-9806: no yaml/contract reads allowed here; topics declared inline.
# onex-topic-allow: values match contract.yaml publish_topics exactly.
TOPIC_PHASE_TRANSITION = "onex.evt.omnimarket.pr-lifecycle-orchestrator-phase-transition.v1"  # onex-topic-allow: contract-declared
TOPIC_COMPLETED = "onex.evt.omnimarket.pr-lifecycle-orchestrator-completed.v1"  # onex-topic-allow: contract-declared
TOPIC_FIXER_DISPATCH_START = (
    "onex.cmd.omnimarket.fixer-dispatch-start.v1"  # onex-topic-allow: contract-declared
)
EVENT_TYPE_FIXER_DISPATCH_START = "omnimarket.fixer-dispatch-start"
TOPIC_PR_LIFECYCLE_START = "onex.cmd.omnimarket.pr-lifecycle-orchestrator-start.v1"  # onex-topic-allow: contract-declared
TOPIC_PR_LIFECYCLE_COMPLETED = "onex.evt.omnimarket.pr-lifecycle-orchestrator-completed.v1"  # onex-topic-allow: contract-declared
TOPIC_PR_LIFECYCLE_FAILED = "onex.evt.omnimarket.pr-lifecycle-orchestrator-failed.v1"  # onex-topic-allow: contract-declared
# OMN-13673: per-PR verification outcome event emitted during the VERIFYING phase.
TOPIC_PR_LIFECYCLE_VERIFICATION_COMPLETED = "onex.evt.omnimarket.pr-lifecycle-verification-completed.v1"  # onex-topic-allow: contract-declared
# OMN-13586 RH-4: repo-health classify/repair fan-out topics.
TOPIC_REPO_HEALTH_CLASSIFY = (
    "onex.cmd.omnimarket.repo-health-classify.v1"  # onex-topic-allow: contract-declared
)
TOPIC_REPO_HEALTH_REPAIR_START = "onex.cmd.omnimarket.repo-health-repair-start.v1"  # onex-topic-allow: contract-declared

# OMN-13831: indeterminate verification outcomes. For a PR whose changed files
# map to a real (code-file) verification target, an indeterminate outcome must
# fail CLOSED — the PR is excluded from the merge set rather than merged
# unverified. These are NEUTRAL (still merge) only for genuine docs-only /
# no-mapping PRs (target == SKIPPED_NO_MAPPING).
_INDETERMINATE_VERIFICATION_OUTCOMES = frozenset(
    {
        EnumVerificationOutcome.VERIFICATION_UNAVAILABLE,
        EnumVerificationOutcome.VERIFICATION_TIMEOUT,
        EnumVerificationOutcome.VERIFICATION_TOOL_ERROR,
    }
)


class ChangedFilesUnavailableError(RuntimeError):
    """Raised when a PR's changed-file list cannot be resolved via ``gh``.

    Distinguishes a *genuinely empty* changed-file list (a successful ``gh`` call
    that returned zero paths → SKIPPED_NO_MAPPING, a neutral skip) from an
    *indeterminate* result (the ``gh`` call failed or timed out even after a
    retry). The orchestrator fails CLOSED on the indeterminate case (OMN-13831):
    a PR whose changed files cannot be enumerated must never be merged on the
    assumption that it is docs-only, because a transient ``gh`` outage would
    otherwise silently let a code PR through unverified.
    """


# ---------------------------------------------------------------------------
# Input / output models
# ---------------------------------------------------------------------------


_DEFAULT_SWEEP_SLEEP_SECONDS = 30 * 60
_DEFAULT_STANDING_SWEEP_PASSES = 17_520  # 365 days at a 30-minute cadence.


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
    verify: bool = Field(
        default=False,
        description="Run verification_sweep per-PR as a pre-merge gate (OMN-7742).",
    )
    verify_timeout_seconds: int = Field(
        default=30,
        ge=1,
        description="Hard per-PR verification timeout in seconds.",
    )
    loop_until_done: bool = Field(
        default=True,
        description=(
            "Keep re-running sweep passes while the org-wide done gate reports "
            "NOT_DONE."
        ),
    )
    max_sweep_passes: int = Field(
        default=_DEFAULT_STANDING_SWEEP_PASSES,
        ge=1,
        description=(
            "Maximum sweep passes for one invocation. The default keeps the "
            "operator sweep standing for roughly one year at the default cadence."
        ),
    )
    sweep_sleep_seconds: int = Field(
        default=_DEFAULT_SWEEP_SLEEP_SECONDS,
        ge=0,
        description="Backoff between NOT_DONE sweep passes.",
    )
    # OMN-14151: merge-queue governor action mode. action_mode and kill_switch
    # are folded into node_pr_arm_gate_compute's ARM/WITHHOLD decision — a
    # single choke point, not a second check the orchestrator could bypass.
    # Both default to the SAFE (zero-mutation) value: an operator must
    # explicitly select ENFORCE *and* explicitly disengage the kill switch
    # before any PR can be armed. This replaces the pre-OMN-14151 posture where
    # dry_run=False alone was sufficient to mutate the merge queue.
    action_mode: EnumArmActionMode = Field(
        default=EnumArmActionMode.REPORT_ONLY,
        description="report_only (default, zero mutation) or enforce (opt-in).",
    )
    merge_queue_mutation_kill_switch: bool = Field(
        default=True,
        description=(
            "Emergency stop, engaged by default. Must be explicitly set False "
            "in addition to action_mode=enforce before any PR can arm."
        ),
    )
    merge_wave_cap: int = Field(
        default=3,
        ge=0,
        description=(
            "Maximum PRs armed in one enforce pass. 0 arms nothing regardless "
            "of action_mode."
        ),
    )
    enable_stall_remediation: bool = Field(
        default=False,
        description=(
            "Opt-in flag for merge-queue stall remediation (dequeue + "
            "re-enqueue an ALREADY-armed PR to re-mint a stuck merge-group "
            "SHA). This is a separate operation from readiness-arming an "
            "unarmed PR, gated by this flag rather than the arm-gate's "
            "per-PR ARM decision. Still requires action_mode=enforce and a "
            "disengaged kill switch."
        ),
    )

    @field_validator("repos", mode="before")
    @classmethod
    def _coerce_repos(cls, value: object) -> str:
        if isinstance(value, list | tuple):
            return ",".join(
                repo for item in value if (repo := _normalize_repo_slug(str(item)))
            )
        return ",".join(
            repo
            for item in str(value or "").split(",")
            if (repo := _normalize_repo_slug(item))
        )


class OrgWideOpenPrRemainderRef(BaseModel):
    """A single org-wide open PR still blocking the sweep-done report (OMN-13318)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(...)
    pr_number: int = Field(...)
    title: str = Field(default="")
    url: str = Field(default="")


class ModelPrLifecycleResult(BaseModel):
    """Result returned by the orchestrator after a sweep run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    prs_inventoried: int = Field(default=0, ge=0)
    prs_merged: int = Field(default=0, ge=0)
    prs_fixed: int = Field(default=0, ge=0)
    prs_skipped: int = Field(default=0, ge=0)
    prs_verified: int = Field(default=0, ge=0)
    # OMN-13673: PRs whose pre-merge verification failed (VERIFICATION_FAILED).
    # These stay open — they are the only verification outcome that blocks merge.
    prs_verification_blocked: int = Field(default=0, ge=0)
    # OMN-13673: per-outcome counts across the 7 verification categories
    # (merged, verification_failed, verification_unavailable, verification_timeout,
    # verification_tool_error, skipped_no_mapping, skipped_by_policy).
    verification_breakdown: dict[str, int] = Field(default_factory=dict)
    final_state: str = Field(default="COMPLETE")
    error_message: str | None = Field(default=None)
    # OMN-13318: org-wide open-PR census is a hard precondition on sweep-done.
    org_wide_open_count: int = Field(
        default=0,
        ge=0,
        description="Org-wide count of open PRs observed at sweep time.",
    )
    org_wide_open_remainders: tuple[OrgWideOpenPrRemainderRef, ...] = Field(
        default_factory=tuple,
        description=(
            "The open PRs that prevented a sweep-done report. Non-empty whenever "
            "final_state is NOT_DONE."
        ),
    )
    # WS-D/D2 (OMN-13940): sweep-level delegation harness counters, summed
    # across all FixResult entries the same way prs_fixed sums prs_dispatched.
    prs_delegated_fix_attempted: int = Field(default=0, ge=0)
    prs_delegated_fix_accepted: int = Field(default=0, ge=0)
    prs_delegated_fix_gate_failed: int = Field(default=0, ge=0)
    prs_delegated_fix_escalated: int = Field(default=0, ge=0)
    delegation_cost_savings_usd: float = Field(default=0.0, ge=0.0)


# ---------------------------------------------------------------------------
# FSM state
# ---------------------------------------------------------------------------


class EnumOrchestratorState(StrEnum):
    IDLE = "IDLE"
    INVENTORYING = "INVENTORYING"
    TRIAGING = "TRIAGING"
    VERIFYING = "VERIFYING"
    MERGING = "MERGING"
    # OMN-12570: after a PR merges, GitHub runs CI on the merge target branch
    # (dev/main). That post-merge "tail" is a DISTINCT verification phase from
    # the branch checks and the merge-group checks — modeled as its own state so
    # a tail failure is attributable to POST_MERGE_TAIL, not to MERGING.
    POST_MERGE_TAIL = "POST_MERGE_TAIL"
    FIXING = "FIXING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


_TERMINAL_STATES = {EnumOrchestratorState.COMPLETE, EnumOrchestratorState.FAILED}

# OMN-13318: sweep-done is gated on an org-wide open-PR count of zero. When the
# FSM reaches COMPLETE but open PRs survive org-wide, the reported final_state is
# downgraded to NOT_DONE so the done-report is refused (and the remainders are
# surfaced). This is a REPORT-level state, distinct from the FSM states above.
_FINAL_STATE_NOT_DONE = "NOT_DONE"

# OMN-12570: map each FSM state to the distinct CI-verification phase it
# represents in the ledger. Branch checks, merge-group checks, and post-merge
# CI tails are SEPARATE phases — the orchestrator stamps the active phase on
# every ledger event and records each phase transition explicitly, so a
# post-merge-tail failure is never confused with a branch-check failure.
_STATE_TO_PHASE: dict[EnumOrchestratorState, EnumPrLifecyclePhase] = {
    EnumOrchestratorState.IDLE: EnumPrLifecyclePhase.INVENTORY,
    EnumOrchestratorState.INVENTORYING: EnumPrLifecyclePhase.INVENTORY,
    EnumOrchestratorState.TRIAGING: EnumPrLifecyclePhase.TRIAGE,
    # Verification and fix remediation both operate on PR branch checks.
    EnumOrchestratorState.VERIFYING: EnumPrLifecyclePhase.BRANCH_CHECKS,
    EnumOrchestratorState.FIXING: EnumPrLifecyclePhase.BRANCH_CHECKS,
    # Merge enqueues into the merge queue → merge-group checks.
    EnumOrchestratorState.MERGING: EnumPrLifecyclePhase.MERGE_GROUP,
    # Post-merge CI tail on the target branch → its own phase.
    EnumOrchestratorState.POST_MERGE_TAIL: EnumPrLifecyclePhase.POST_MERGE_TAIL,
    EnumOrchestratorState.COMPLETE: EnumPrLifecyclePhase.TERMINAL,
    EnumOrchestratorState.FAILED: EnumPrLifecyclePhase.TERMINAL,
}


def _phase_for_state(state: EnumOrchestratorState) -> EnumPrLifecyclePhase:
    """Return the ledger phase a given FSM state belongs to (OMN-12570)."""
    return _STATE_TO_PHASE[state]


@dataclass
class _SweepState:
    """Mutable sweep state tracked across phases."""

    fsm: EnumOrchestratorState = EnumOrchestratorState.IDLE
    # OMN-12570: the CI-verification phase the orchestrator is currently in.
    # Stamped onto every ledger event recorded while in this phase, and updated
    # only through explicit recorded transitions (never inferred from logs).
    phase: EnumPrLifecyclePhase = EnumPrLifecyclePhase.INVENTORY
    prs_inventoried: int = 0
    prs_merged: int = 0
    prs_fixed: int = 0
    prs_skipped: int = 0
    prs_verified: int = 0
    error_message: str | None = None
    # WS-D/D2 (OMN-13940): delegation harness counters, aggregated the same
    # way prs_fixed sums FixResult.prs_dispatched.
    prs_delegated_fix_attempted: int = 0
    prs_delegated_fix_accepted: int = 0
    prs_delegated_fix_gate_failed: int = 0
    prs_delegated_fix_escalated: int = 0
    delegation_cost_savings_usd: float = 0.0

    # Inter-phase data
    inventory_result: InventoryResult | None = None
    triage_result: PrTriageResult | None = None
    reducer_result: ReducerResult | None = None

    # OMN-13318: org-wide open-PR census captured during INVENTORYING. The
    # sweep-done report is refused while open_count > 0 (or the census failed).
    org_wide_open: Any | None = None

    # OMN-13673: per-PR verification outcome captured during the VERIFYING
    # phase, keyed by (repo, pr_number) -> EnumVerificationOutcome.value. Only
    # VERIFICATION_FAILED blocks the PR's auto-merge; every other outcome is a
    # neutral skip that still proceeds to MERGING.
    verification_outcomes: dict[tuple[str, int], str] = field(default_factory=dict)
    prs_verification_blocked: int = 0


# ---------------------------------------------------------------------------
# Stub sub-handler doubles.
#
# As of OMN-13984 these are NOT silent import-failure fallbacks for the core
# sweep handlers (inventory/triage/reducer/merge/fix): an ImportError on any of
# those now RAISES in _ensure_sub_handlers instead of degrading to a 0-result
# "successful" sweep (a false positive that could mask real merge/fix work).
# They remain for two legitimate uses only:
#   (a) worktree-prune GC — the one intentional optional (OMN-13859: prune
#       degrading must never fail a sweep); its ImportError fallback is kept.
#   (b) lightweight test doubles imported directly by the unit tests.
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


class _StubPruneHandler:
    """Stub matching HandlerWorktreePrune.handle(command) signature.

    Returns a no-op result so the POST_MERGE_TAIL prune step is inert when the
    worktree-prune node is unavailable (OMN-13859).
    """

    async def handle(self, command: Any) -> Any:
        logger.warning("[PR-LIFECYCLE-ORCH] prune stub called (sub-node not wired)")
        return None


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

_TICKET_ID_PATTERN = re.compile(r"\bOMN-\d+\b", re.IGNORECASE)
_UNKNOWN_OCC_MERGE_SHA = "unknown-occ-merge-sha"
_RECEIPT_GATE_CHECK_NAME = "verify / verify"
# OMN-13990 follow-up: occ-preflight is a SEPARATE required check from the
# receipt gate (omnibase_core occ-preflight.yml, job "eligibility") and fails
# on the exact same "green-except-OCC-companion" signature — a PR whose only
# red check is the OCC eligibility gate. Before this fix, only
# _RECEIPT_GATE_CHECK_NAME was recognized here, so an occ-preflight-only
# failure fell through to the generic RED/CODE_FAILURE branch below and never
# reached the autobind arm (omninode_infra#2238 / omnibase_infra#2238 class).
_OCC_PREFLIGHT_CHECK_NAME = "occ-preflight / eligibility"
_OCC_EVIDENCE_CHECK_NAMES = frozenset(
    {_RECEIPT_GATE_CHECK_NAME, _OCC_PREFLIGHT_CHECK_NAME}
)
# OMN-13990 follow-up (round 2): a live sweep of the still-blocked PRs found
# the widened subset match above STILL never fires, because a second,
# cosmetic check rides along in the failed set — e.g. omninode_infra#579's
# failed_check_names is {"verify / verify", "Enable Auto-Merge"}.
# "Enable Auto-Merge" fails BY DESIGN on every PR today (org-wide auto-merge
# is off) and is verified NOT a required status check on any repo's `dev`
# branch protection (checked live 2026-07-08: omnimarket, omnibase_infra,
# omniclaude, omninode_infra all omit it from
# `branches/dev/protection/required_status_checks`). Excluding it from the
# OCC-evidence-signature comparison (below) lets the subset match see through
# it. This denylist is deliberately narrow — it does NOT include checks like
# `call-reject-skip-token` (omninode_infra#578's second failing check), which
# IS a required context; a required-but-flaky check needs a CI rerun, not a
# classifier exclusion (see the existing `_FLAKY_INFRA_CHECK_SUBSTRINGS` /
# `_has_flaky_failure_evidence` path below for that case).
_COSMETIC_NON_REQUIRED_CHECK_NAMES = frozenset({"Enable Auto-Merge"})
_DEFAULT_GITHUB_OWNER = "OmniNode-ai"


def _normalize_repo_slug(value: str) -> str:
    """Return a GitHub ``OWNER/REPO`` slug for user-facing repo filters."""
    repo = value.strip()
    if not repo:
        return ""
    if "/" in repo:
        return repo
    return f"{_DEFAULT_GITHUB_OWNER}/{repo}"


# ---------------------------------------------------------------------------
# Machine failure-signature constants (OMN-13987 CP1)
#
# _block_reason_for_fix classifies off the machine failure signature
# (failed_check_names + triage category + ticket presence), never off the
# human-readable block_reason prose. These constants drive the three arms that
# were previously dead because nothing emitted their machine enum literal.
# ---------------------------------------------------------------------------

# deploy-gate required-status-check name substring. Covers the reusable-workflow
# form ("deploy-gate / deploy-gate") and the inline form ("deploy-gate").
_DEPLOY_GATE_CHECK_SIGNATURE = "deploy-gate"

# Check-name substrings that indicate a GENUINE code failure (lint / type /
# test / build). If ANY failed check matches one of these, the failure is never
# treated as a cheap flaky rerun — it routes to CODE_FAILURE (delegatable fix).
_CODE_SIGNAL_CHECK_SUBSTRINGS: tuple[str, ...] = (
    "lint",
    "ruff",
    "mypy",
    "type-check",
    "typecheck",
    "test",
    "pytest",
    "format",
    "compile",
    "build",
    "coverage",
    "pre-commit",
)

# Check-name substrings for KNOWN flaky/infra failures that a bare re-run
# (``gh run rerun --failed``) can clear without any code change. CI_FAILURE is
# emitted ONLY when EVERY failed check matches one of these AND none match a
# code-signal substring — a deliberately narrow, fail-safe guard so a real
# lint/type/test failure is never silently rerun instead of fixed.
_FLAKY_INFRA_CHECK_SUBSTRINGS: tuple[str, ...] = (
    "runner",
    "self-hosted",
    "fleet",
    "flaky",
    "network",
    "timeout",
    "timed out",
    "set up job",
    "set up runner",
    "queue",
    "infrastructure",
    "provision",
)


def _has_deploy_gate_failure(failed_check_names: tuple[str, ...]) -> bool:
    """True iff a deploy-gate required check is among the failed checks."""
    return any(
        _DEPLOY_GATE_CHECK_SIGNATURE in name.lower() for name in failed_check_names
    )


def _is_flaky_infra_only(failed_check_names: tuple[str, ...]) -> bool:
    """True iff every failed check is a known flaky/infra check (rerunnable).

    Fail-safe: returns False when there are no failed check names, or when any
    failed check looks like a genuine code failure (lint/type/test/build). Only
    when the whole failed-check set is unambiguously flaky/infra does a cheap
    ``gh run rerun --failed`` become the right routed action.
    """
    if not failed_check_names:
        return False
    lowered = [name.lower() for name in failed_check_names]
    if any(sub in name for name in lowered for sub in _CODE_SIGNAL_CHECK_SUBSTRINGS):
        return False
    return all(
        any(sub in name for sub in _FLAKY_INFRA_CHECK_SUBSTRINGS) for name in lowered
    )


def _has_code_signal_failure(failed_check_names: tuple[str, ...]) -> bool:
    """True when any failed check name indicates code that must be fixed."""
    return any(
        sub in name.lower()
        for name in failed_check_names
        for sub in _CODE_SIGNAL_CHECK_SUBSTRINGS
    )


def _has_flaky_failure_evidence(pr: TriageRecord) -> bool:
    """True when inventory found hard network/clone evidence for failed checks."""
    return bool(tuple(e.strip() for e in pr.failed_check_flaky_evidence if e.strip()))


def _map_ci_status(pr_state: Any) -> str:
    """Map ModelPrState fields to orchestrator-internal checks_status string."""
    if getattr(pr_state, "ci_passing", None) is True:
        return "success"
    if getattr(pr_state, "ci_passing", None) is False:
        return "failure"
    return "unknown"


def _failed_check_names(pr_state: Any) -> tuple[str, ...]:
    """Return failed check names from a ModelPrState-like object."""
    names: list[str] = []
    for check in getattr(pr_state, "check_runs", ()) or ():
        conclusion = str(getattr(check, "conclusion", "") or "").lower()
        if conclusion in {"failure", "cancelled", "timed_out", "action_required"}:
            name = str(getattr(check, "name", "") or "").strip()
            if name:
                names.append(name)
    return tuple(sorted(set(names)))


def _failed_check_flaky_evidence(pr_state: Any) -> tuple[str, ...]:
    """Return machine flaky evidence collected for failed checks."""
    evidence: list[str] = []
    for check in getattr(pr_state, "check_runs", ()) or ():
        conclusion = str(getattr(check, "conclusion", "") or "").lower()
        if conclusion not in {"failure", "cancelled", "timed_out", "action_required"}:
            continue
        evidence.extend(
            str(item).strip()
            for item in getattr(check, "flaky_failure_evidence", ()) or ()
            if str(item).strip()
        )
    return tuple(sorted(set(evidence)))


def _extract_ticket_ids(*values: str) -> tuple[str, ...]:
    """Extract canonical OMN ticket IDs from PR title/branch metadata."""
    found: set[str] = set()
    for value in values:
        found.update(
            match.group(0).upper() for match in _TICKET_ID_PATTERN.finditer(value)
        )
    return tuple(sorted(found))


def _map_review_status(pr_state: Any) -> str:
    """Map ModelPrState.review_decision to orchestrator-internal review_status."""
    decision: str = getattr(pr_state, "review_decision", "") or ""
    return _REVIEW_STATUS_MAP.get(decision.upper(), "unknown")


def _synthetic_merge_group_sha(
    run_id: str,
    repo: str,
    pr_number: int,
    *,
    attempt: int = 1,
) -> str:
    """Derive a deterministic synthetic merge-group SHA for a PR/attempt.

    GitHub mints the real merge-group commit SHA at enqueue time; the
    orchestrator does not always observe it synchronously. This deterministic
    surrogate keys the ledger entry's merge-group provenance so reruns
    (attempt>1) are distinguishable, and so the same (run_id, repo, pr_number,
    attempt) always derives the same value — preserving reconstructability.
    """
    seed = f"{run_id}:{repo}:{pr_number}:{attempt}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    return f"mg-{digest}-{attempt}"


def _orch_checks_to_ci_status(checks_status: str) -> str:
    """Convert orchestrator checks_status to triage node's ci_status vocabulary."""
    return _CI_STATUS_MAP.get(checks_status.lower(), "unknown")


def _block_reason_for_fix(pr: TriageRecord) -> Any:
    """Map triage output to the fix-effect routing enum.

    Triage ``block_reason`` is a human-readable sentence, not the machine enum
    consumed by node_pr_lifecycle_fix_effect. Preserve explicit enum values
    when they are supplied, otherwise derive routing from deterministic triage
    fields so PR-polish-worthy failures do not collapse to CI reruns.
    """
    from omnimarket.nodes.node_pr_lifecycle_fix_effect.models.model_fix_command import (
        EnumPrBlockReason,
    )

    block_reason = (pr.block_reason or "").strip()
    if block_reason:
        try:
            return EnumPrBlockReason(block_reason)
        except ValueError:
            pass

    reason_lower = block_reason.lower()
    failed_check_names = tuple(name.strip() for name in pr.failed_check_names)
    failed_check_names_lower = tuple(name.lower() for name in failed_check_names)

    if pr.category == EnumPrCategory.CONFLICTED:
        return EnumPrBlockReason.CONFLICT

    # OMN-13987 CP1: a failed deploy-gate check means the PR's own OCC deploy
    # contract is missing (or the trivial-infra fast-path applies). Route to the
    # contract auto-create arm. Keyed on the machine check name, so it never
    # collides with the receipt-gate (OCC_DEPENDENCY) or genuine code failures.
    if _has_deploy_gate_failure(failed_check_names):
        return EnumPrBlockReason.DEPLOY_GATE_CONTRACT_NOT_FOUND

    # OMN-13990 follow-up (round 2): drop cosmetic non-required checks (e.g.
    # "Enable Auto-Merge", which fails BY DESIGN org-wide) before comparing
    # against the OCC-evidence signature — see _COSMETIC_NON_REQUIRED_CHECK_NAMES
    # above. Scoped to this comparison only; the raw failed_check_names tuple
    # (still including cosmetic entries) is used unchanged everywhere else in
    # this function.
    evidence_signature_checks = (
        set(failed_check_names) - _COSMETIC_NON_REQUIRED_CHECK_NAMES
    )
    if (
        evidence_signature_checks
        and evidence_signature_checks <= _OCC_EVIDENCE_CHECK_NAMES
    ):
        # OMN-13987 CP1 (extended, OMN-13990 follow-up): a failure set drawn
        # ENTIRELY from {receipt gate, occ-preflight} plus cosmetic checks —
        # either alone, or both together — has a ticket is the
        # Evidence-Source-autobind class (OMN-13317 — the "green-except-OCC-
        # companion" PR). Route to the cheap machine rebind; the adapter is
        # self-guarding (no-ops when Evidence-Source is already an OCC
        # source). Without a ticket, autobind cannot run, so genuine receipt
        # failures keep the existing agent RECEIPT_FAILURE path.
        if pr.ticket_ids:
            return EnumPrBlockReason.RECEIPT_EVIDENCE_SOURCE_AUTOBIND
        return EnumPrBlockReason.RECEIPT_FAILURE

    if "coderabbit" in reason_lower or any(
        "coderabbit" in name for name in failed_check_names_lower
    ):
        return EnumPrBlockReason.CODERABBIT
    if pr.category == EnumPrCategory.NEEDS_REVIEW:
        return EnumPrBlockReason.CHANGES_REQUESTED

    # OMN-13987 CP1: a RED PR whose failed checks are ALL known flaky/infra
    # (rerunnable) and NONE look like a genuine lint/type/test failure → a cheap
    # CI rerun. Deliberately narrow + fail-safe: any code-signal check present
    # falls through to CODE_FAILURE below.
    if pr.category == EnumPrCategory.RED and (
        _is_flaky_infra_only(failed_check_names)
        or (
            _has_flaky_failure_evidence(pr)
            and not _has_code_signal_failure(failed_check_names)
        )
    ):
        return EnumPrBlockReason.CI_FAILURE

    if pr.category == EnumPrCategory.RED:
        return EnumPrBlockReason.CODE_FAILURE
    return EnumPrBlockReason.CODE_FAILURE


# Maps a triage EnumPrCategory to the fixer-dispatcher EnumStallCategory wire
# literal (OMN-13987 CP2). Values MUST equal EnumStallCategory members in
# node_fixer_dispatcher.models.model_fixer_dispatch — asserted by a test rather
# than importing another node's model package at runtime (repo boundary rule).
# Only RED and CONFLICTED have machine auto-fix routes (node_ci_fix_effect and
# node_conflict_hunk_effect respectively); every other category maps to
# ``unknown`` so the dispatcher escalates deterministically.
_PR_CATEGORY_TO_STALL_CATEGORY: dict[EnumPrCategory, str] = {
    EnumPrCategory.RED: "red",
    EnumPrCategory.CONFLICTED: "conflicted",
}


def _stall_category_for_dispatch(category: EnumPrCategory) -> str:
    """Map a triage EnumPrCategory to a fixer-dispatcher EnumStallCategory literal.

    node_fixer_dispatcher routes on machine EnumStallCategory literals
    (red / conflicted / behind / deploy_gate); publishing the human-readable
    ``block_reason`` prose always missed the routing table and forced every
    dispatch to escalate — leaving node_ci_fix_effect / node_conflict_hunk_effect
    dead. RED→``red`` reaches node_ci_fix_effect and CONFLICTED→``conflicted``
    reaches node_conflict_hunk_effect; unmapped categories return ``unknown``.
    """
    return _PR_CATEGORY_TO_STALL_CATEGORY.get(category, "unknown")


def _render_verification_breakdown(
    outcomes: Any,
) -> dict[str, int]:
    """Render the 7-category verification breakdown as a zero-filled count map.

    Every ``EnumVerificationOutcome`` member is present so the render always
    surfaces all 7 categories (merged, verification_failed,
    verification_unavailable, verification_timeout, verification_tool_error,
    skipped_no_mapping, skipped_by_policy), even when a category has zero PRs.
    ``outcomes`` is an iterable of ``EnumVerificationOutcome.value`` strings.
    """
    breakdown: dict[str, int] = {member.value: 0 for member in EnumVerificationOutcome}
    for value in outcomes:
        key = str(value)
        if key in breakdown:
            breakdown[key] += 1
    return breakdown


# ---------------------------------------------------------------------------
# Orchestrator handler
# ---------------------------------------------------------------------------


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
        prune: ProtocolPruneHandler | None = None,
        arm_gate: ProtocolArmGateHandler | None = None,
        event_bus: ProtocolEventBusPublisher,
        ledger_store: ProtocolPrLedgerStore | None = None,
        projection_db: ProtocolProjectionDatabaseSync | None = None,
        occ_stamp_readback: ProtocolOccStampReadback | None = None,
    ) -> None:
        self._topic_phase_transition = TOPIC_PHASE_TRANSITION
        self._topic_completed = TOPIC_COMPLETED
        self._topic_fixer_dispatch_start = TOPIC_FIXER_DISPATCH_START
        self._topic_verification_completed = TOPIC_PR_LIFECYCLE_VERIFICATION_COMPLETED

        self._inventory = inventory
        self._triage = triage
        self._reducer = reducer
        self._merge = merge
        self._fix = fix
        # OMN-13859: worktree-prune effect invoked in POST_MERGE_TAIL, one
        # command per merged (ticket, repo). Optional like the other sub-handlers.
        self._prune = prune
        # OMN-14151: sole ARM/WITHHOLD decider for the merge fanout. Optional
        # like the other sub-handlers; _ensure_sub_handlers wires the real
        # HandlerPrArmGate for live runs.
        self._arm_gate = arm_gate
        self._event_bus = event_bus
        # Durable, reconstructable PR-ledger projection (OMN-12569). Defaults to
        # the in-memory store for local/test runs; the runtime injects a
        # ProjectionDatabasePrLedgerStore so the ledger lands in the
        # control-plane durable surface. The ledger is a DERIVED projection —
        # source events are recorded as they happen and folded into the store.
        self._ledger_store: ProtocolPrLedgerStore = (
            ledger_store if ledger_store is not None else InMemoryPrLedgerStore()
        )
        # OMN-13321 / F5: raw projection database for the per-iteration,
        # user-readable PR ledger emitted by the state reducer. None in
        # local/test runs (the reducer then stays a pure classifier); the
        # runtime injects the control-plane projection database so a clean
        # ledger row lands per PR per iteration.
        self._projection_db: ProtocolProjectionDatabaseSync | None = projection_db
        # OMN-14191: independent OCC-stamp read-back gate. prs_fixed is counted
        # per fix arm ONLY after this reads the ACTUAL pushed OCC companion + the
        # product PR body back (Piece-2 parser, live gh state) and confirms the
        # stamp landed — never on the fix handler's self-reported return
        # (CLAUDE.md Rule 3). Defaults fail-closed (proves nothing -> counts
        # nothing); _ensure_sub_handlers wires the live OccStampReadback for real
        # runs. This generalizes the OMN-14173 autobind read-back to every arm
        # and closes OMN-14174's dispatch-vs-effect over-count.
        self._occ_stamp_readback: ProtocolOccStampReadback = (
            occ_stamp_readback
            if occ_stamp_readback is not None
            else _UnverifiedOccStampReadback()
        )
        self._occ_stamp_readback_injected = occ_stamp_readback is not None

    def _record_ledger_event(
        self,
        *,
        kind: EnumPrLedgerEventKind,
        run_id: str,
        correlation_id: UUID,
        repo: str,
        pr_number: int,
        orchestrator_action: EnumOrchestratorAction,
        phase: EnumPrLifecyclePhase,
        head_sha: str | None = None,
        workflow_run_id: int | None = None,
        merge_group_sha: str | None = None,
        conclusion: EnumPrLedgerConclusion | None = None,
    ) -> None:
        """Fold one source event into the durable PR-ledger projection.

        The ledger is a derived projection — never authoritative truth on its
        own — so recording is best-effort and must never abort a sweep. Each
        event carries full provenance (workflow run, merge-group SHA, branch
        SHA, orchestrator action, timestamp).

        OMN-12570: the caller passes the active FSM ``phase`` so the event is
        attributed to the phase it was produced in. Phase comes from the
        orchestrator's current recorded state — it is never inferred from the
        orchestrator_action after the fact.
        """
        try:
            event = ModelPrLedgerSourceEvent(
                kind=kind,
                run_id=run_id,
                correlation_id=correlation_id,
                repo=repo,
                pr_number=pr_number,
                head_sha=head_sha,
                workflow_run_id=workflow_run_id,
                merge_group_sha=merge_group_sha,
                conclusion=conclusion,
                orchestrator_action=orchestrator_action,
                phase=phase,
                observed_at=datetime.now(UTC).isoformat(),
            )
            apply_pr_ledger_event(event, store=self._ledger_store)
        except Exception as exc:
            logger.warning(
                "[PR-LIFECYCLE-ORCH] failed to record ledger event kind=%s "
                "repo=%s pr=%s: %s",
                kind,
                repo,
                pr_number,
                exc,
            )

    async def _transition_phase(
        self,
        state: _SweepState,
        to_state: EnumOrchestratorState,
        *,
        run_id: str,
        correlation_id: UUID,
    ) -> None:
        """Make an explicit, recorded FSM phase transition (OMN-12570).

        One method owns *all* phase changes so that every transition is:
          1. recorded durably in the ledger transition log at transition time
             (only when the ledger phase actually changes — INVENTORYING and
             IDLE share the INVENTORY phase, for example);
          2. reflected in ``state.fsm`` and ``state.phase`` so subsequent ledger
             events are stamped with the correct phase;
          3. published on the phase-transition topic for live observers.

        Recording the transition explicitly — rather than letting consumers
        infer phase from log lines — is the core OMN-12570 requirement.
        """
        from_state = state.fsm
        from_phase = state.phase
        to_phase = _phase_for_state(to_state)

        if to_phase is not from_phase:
            try:
                record_phase_transition(
                    ModelPrLifecyclePhaseTransition(
                        run_id=run_id,
                        correlation_id=correlation_id,
                        from_phase=from_phase,
                        to_phase=to_phase,
                        recorded_at=datetime.now(UTC).isoformat(),
                    ),
                    store=self._ledger_store,
                )
            except Exception as exc:
                # The ledger is a derived projection; a recording failure must
                # never abort a sweep. The transition still drives the FSM.
                logger.warning(
                    "[PR-LIFECYCLE-ORCH] failed to record phase transition "
                    "%s -> %s: %s",
                    from_phase.value,
                    to_phase.value,
                    exc,
                )

        state.fsm = to_state
        state.phase = to_phase
        await self._publish_phase_event(
            from_state.value, to_state.value, correlation_id
        )

    def _next_iteration(self, sweep_id: str) -> int:
        """Resolve the next per-sweep ledger iteration index (OMN-13321 / F5).

        Queries the durable ledger projection for the highest iteration
        already recorded for this ``sweep_id`` and returns max+1, so two
        consecutive orchestrator passes that share a sweep_id append distinct
        row sets (iteration 0, 1, ...) rather than overwriting each other.
        Returns 0 when no projection database is wired or no rows exist yet.
        """
        if self._projection_db is None:
            return 0
        rows = self._projection_db.query(
            PR_LEDGER_PROJECTION_TABLE, {"sweep_id": sweep_id}
        )
        if not rows:
            return 0
        # Projection rows are dict[str, object]; iteration is stored as an int
        # (or its serialized form). str() round-trips both safely for int().
        return max(int(str(row["iteration"])) for row in rows) + 1

    def ledger(self, run_id: str) -> ModelPrLedger:
        """Return the durable PR-ledger projection for a sweep run.

        The projection is reconstructable from its source events; this accessor
        returns the materialized view from the durable store.
        """
        return self._ledger_store.load(run_id)

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
            except ImportError as exc:
                raise RuntimeError(
                    "PR-lifecycle inventory sub-handler failed to import; refusing "
                    "to run a merge sweep on a silent no-op stub that would report "
                    "0 PRs as a successful pass. Fix the omnimarket install/"
                    "packaging drift instead of degrading silently (OMN-13984)."
                ) from exc
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
            except ImportError as exc:
                raise RuntimeError(
                    "PR-lifecycle triage sub-handler failed to import; refusing to "
                    "run a merge sweep on a silent no-op stub that would classify "
                    "0 PRs as a successful pass. Fix the omnimarket install/"
                    "packaging drift instead of degrading silently (OMN-13984)."
                ) from exc
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
            except ImportError as exc:
                raise RuntimeError(
                    "PR-lifecycle state-reducer sub-handler failed to import; "
                    "refusing to run a merge sweep on a silent no-op stub that "
                    "would produce 0 merge/fix intents as a successful pass. Fix "
                    "the omnimarket install/packaging drift instead of degrading "
                    "silently (OMN-13984)."
                ) from exc
        if self._merge is None:
            try:
                from omnimarket.nodes.node_pr_lifecycle_merge_effect.handlers.adapter_github_merge_queue import (
                    GitHubMergeQueueAdapter,
                )
                from omnimarket.nodes.node_pr_lifecycle_merge_effect.handlers.handler_pr_lifecycle_merge import (
                    HandlerPrLifecycleMerge,
                )

                merge_handler = HandlerPrLifecycleMerge(
                    github_adapter=GitHubMergeQueueAdapter(),
                )
                self._check_protocol_conformance(
                    merge_handler, ProtocolMergeHandler, "merge"
                )
                self._merge = merge_handler
            except ImportError as exc:
                raise RuntimeError(
                    "PR-lifecycle merge sub-handler failed to import; refusing to "
                    "run a merge sweep on a silent no-op stub that would report "
                    "0 merges as a successful pass. Fix the omnimarket install/"
                    "packaging drift instead of degrading silently (OMN-13984)."
                ) from exc
        if self._fix is None:
            try:
                from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_delegated_fix import (
                    DelegatedFixAdapter,
                )
                from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_github_cli import (
                    GitHubCliAdapter,
                )
                from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_occ_autobind import (
                    OccAutobindAdapter,
                )
                from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_occ_contract import (
                    OccContractAdapter,
                )
                from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_pr_polish_dispatch import (
                    PrPolishDispatchAdapter,
                )
                from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.adapter_two_strike_store import (
                    JsonFileTwoStrikeStore,
                )
                from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.handler_pr_lifecycle_fix import (
                    HandlerPrLifecycleFix,
                )
                from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers.occ_companion_verifier import (
                    OccCompanionVerifier,
                )

                # WS-D/D2 (OMN-13940): DelegatedFixAdapter + JsonFileTwoStrikeStore
                # are wired explicitly (not left to HandlerPrLifecycleFix's
                # test-convenience noop/in-memory defaults) so real merge-sweep
                # runs actually attempt the delegated path and the two-strike
                # counter survives across ticks.
                fix_handler = HandlerPrLifecycleFix(
                    github_adapter=GitHubCliAdapter(),
                    agent_dispatch_adapter=PrPolishDispatchAdapter(),
                    occ_contract_adapter=OccContractAdapter(),
                    occ_autobind_adapter=OccAutobindAdapter(),
                    # OMN-14173: live read-back verifier so prs_fixed is gated on
                    # a CONFIRMED pushed OCC companion, not on fix_applied. Without
                    # this the autobind arm reported prs_fixed while authoring zero
                    # companions (merge_sweep --fix-only false-success).
                    occ_companion_verifier=OccCompanionVerifier(),
                    delegation_fix_adapter=DelegatedFixAdapter(),
                    two_strike_store=JsonFileTwoStrikeStore(),
                )
                self._check_protocol_conformance(fix_handler, ProtocolFixHandler, "fix")
                self._fix = fix_handler
            except ImportError as exc:
                raise RuntimeError(
                    "PR-lifecycle fix sub-handler failed to import; refusing to "
                    "run a merge sweep on a silent no-op stub that would report "
                    "0 fixes dispatched as a successful pass (masking the real "
                    "delegated-fix path). Fix the omnimarket install/packaging "
                    "drift instead of degrading silently (OMN-13984)."
                ) from exc
        if self._prune is None:
            # OMN-13859: worktree-prune effect. Falls back to a no-op stub when
            # the node is unavailable so the merge sweep never fails for lack of
            # worktree GC.
            try:
                from omnimarket.nodes.node_pr_lifecycle_worktree_prune_effect.handlers.adapter_git_worktree import (
                    GitWorktreeAdapter,
                )
                from omnimarket.nodes.node_pr_lifecycle_worktree_prune_effect.handlers.handler_worktree_prune import (
                    HandlerWorktreePrune,
                )

                prune_handler = HandlerWorktreePrune(git_adapter=GitWorktreeAdapter())
                self._check_protocol_conformance(
                    prune_handler, ProtocolPruneHandler, "prune"
                )
                self._prune = prune_handler
            except ImportError:
                self._prune = _StubPruneHandler()
        if self._arm_gate is None:
            # OMN-14151: the arm-gate is a pure compute with zero optional
            # dependencies — no ImportError fallback stub is needed, but a
            # missing import must still fail loudly (never a silent no-op
            # decider) per OMN-13984's "no silent successful no-op" rule.
            from omnimarket.nodes.node_pr_arm_gate_compute.handlers.handler_arm_gate import (
                HandlerPrArmGate,
            )

            arm_gate_handler = HandlerPrArmGate()
            self._check_protocol_conformance(
                arm_gate_handler, ProtocolArmGateHandler, "arm_gate"
            )
            self._arm_gate = arm_gate_handler
        # OMN-14191: wire the live OCC-stamp read-back for real runs so prs_fixed
        # is gated on a CONFIRMED landed OCC companion (read back over live gh
        # state), not on the fix handler's dispatch-time self-report. Skipped when
        # an explicit read-back was injected (tests inject a hermetic double).
        if not self._occ_stamp_readback_injected:
            from omnimarket.nodes.node_pr_lifecycle_orchestrator.handlers.occ_stamp_readback import (
                OccStampReadback,
            )

            self._occ_stamp_readback = OccStampReadback()

    async def handle(
        self,
        command: ModelPrLifecycleStartCommand,
    ) -> ModelPrLifecycleResult:
        """Run the PR lifecycle sweep loop and persist each pass result.

        Writes ``$ONEX_STATE_DIR/merge-sweep/{run_id}/result.json`` on both
        success and failure paths. The merge_sweep skill (v4.0.0+) polls this
        file to determine orchestrator completion.
        """
        result: ModelPrLifecycleResult | None = None
        for pass_index in range(1, command.max_sweep_passes + 1):
            try:
                logger.info(
                    "[PR-LIFECYCLE-ORCH] sweep pass %d/%d run_id=%s",
                    pass_index,
                    command.max_sweep_passes,
                    command.run_id,
                )
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
            if result.final_state != _FINAL_STATE_NOT_DONE:
                return result
            if (
                not command.loop_until_done
                or command.dry_run
                or command.inventory_only
                or not self._should_continue_sweep_loop(result)
                or pass_index >= command.max_sweep_passes
            ):
                return result

            logger.info(
                "[PR-LIFECYCLE-ORCH] sweep pass %d/%d reported NOT_DONE; "
                "sleeping %ds before re-inventory",
                pass_index,
                command.max_sweep_passes,
                command.sweep_sleep_seconds,
            )
            if command.sweep_sleep_seconds > 0:
                await asyncio.sleep(command.sweep_sleep_seconds)

        assert result is not None
        return result

    @staticmethod
    def _should_continue_sweep_loop(result: ModelPrLifecycleResult) -> bool:
        """Return whether another pass can still act on the NOT_DONE result."""
        if result.final_state != _FINAL_STATE_NOT_DONE:
            return False
        if result.prs_inventoried > 0:
            return True
        if result.prs_merged > 0 or result.prs_fixed > 0 or result.prs_verified > 0:
            return True
        return False

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
            # Phase: INVENTORYING (INVENTORY ledger phase)
            await self._transition_phase(
                state,
                EnumOrchestratorState.INVENTORYING,
                run_id=command.run_id,
                correlation_id=command.correlation_id,
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
            # OMN-13318: capture the org-wide open-PR census now so the
            # sweep-done report can be refused while open PRs survive in any
            # repo (the repo-by-repo inventory above can miss them).
            state.org_wide_open = self._collect_org_wide_open_prs()
            logger.info(
                "[PR-LIFECYCLE-ORCH] inventory completed: %d PRs (org-wide open=%s)",
                inv_result.total_collected,
                getattr(state.org_wide_open, "open_count", "n/a"),
            )
            # Ledger (OMN-12569): record one PR_INVENTORIED source event per
            # collected PR so the projection captures run id + branch SHA from
            # the start of the lifecycle.
            for pr in inv_result.prs:
                self._record_ledger_event(
                    kind=EnumPrLedgerEventKind.PR_INVENTORIED,
                    run_id=command.run_id,
                    correlation_id=command.correlation_id,
                    repo=pr.repo,
                    pr_number=pr.pr_number,
                    head_sha=pr.head_sha,
                    orchestrator_action=EnumOrchestratorAction.INVENTORY,
                    phase=state.phase,
                )
            await self._remediate_stalled_queue_prs(command, inv_result)

            if command.inventory_only:
                await self._transition_phase(
                    state,
                    EnumOrchestratorState.COMPLETE,
                    run_id=command.run_id,
                    correlation_id=command.correlation_id,
                )
                return self._build_result(state, command.correlation_id)

            # Phase: TRIAGING
            await self._transition_phase(
                state,
                EnumOrchestratorState.TRIAGING,
                run_id=command.run_id,
                correlation_id=command.correlation_id,
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
            # OMN-13321 / F5: emit one durable, user-readable ledger row per
            # PR for THIS iteration through the state reducer. iteration is
            # resolved from the durable projection so consecutive passes that
            # share a sweep_id (== run_id) append distinct rows.
            ledger_iteration = self._next_iteration(command.run_id)
            reducer_result = await self._reducer.handle(
                correlation_id=command.correlation_id,
                classified=triage_result.classified,
                dry_run=command.dry_run,
                inventory_only=command.inventory_only,
                fix_only=command.fix_only,
                merge_only=command.merge_only,
                projection_db=self._projection_db,
                sweep_id=command.run_id if self._projection_db is not None else None,
                iteration=ledger_iteration,
            )
            state.reducer_result = reducer_result
            self._write_occ_dependency_edges_file(
                command.run_id,
                self._occ_dependency_edges(
                    triage_result=triage_result,
                    reducer_result=reducer_result,
                    occ_merge_sha=self._resolve_occ_merge_sha(
                        triage_result=triage_result,
                        reducer_result=reducer_result,
                    ),
                ),
            )

            # Build per-intent sets. Computed BEFORE the dry_run short-circuit so
            # the VERIFYING phase can materialize its 7-category breakdown on the
            # dry-run path too (OMN-13673).
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

            # Phase: VERIFYING (OMN-13673 / OMN-7742). When verify=True and there
            # are merge-ready PRs, run a per-PR pre-merge verification gate before
            # MERGING. Only VERIFICATION_FAILED blocks that PR (it stays open);
            # every other outcome (passed/unavailable/timeout/tool_error/
            # no_mapping/by_policy) is a NEUTRAL skip that still proceeds to
            # MERGING. A failure in one PR never blocks the rest of the batch.
            # Runs in BOTH dry_run and live paths so the breakdown is always
            # materialized — in dry_run the gate classifies each PR as
            # SKIPPED_BY_POLICY without executing real probes. This replaces the
            # prior hard RuntimeError raise: refusing a PR's merge is now
            # STATE-BASED (state.verification_outcomes), never a raise.
            if command.verify and merge_prs and not command.fix_only:
                await self._transition_phase(
                    state,
                    EnumOrchestratorState.VERIFYING,
                    run_id=command.run_id,
                    correlation_id=command.correlation_id,
                )
                merge_prs = await self._run_verification(
                    merge_prs=merge_prs,
                    command=command,
                    state=state,
                )

            if command.dry_run:
                # dry_run: record intents but do not execute
                state.prs_skipped = len(reducer_result.intents)
                await self._transition_phase(
                    state,
                    EnumOrchestratorState.COMPLETE,
                    run_id=command.run_id,
                    correlation_id=command.correlation_id,
                )
                return self._build_result(state, command.correlation_id)

            state.prs_skipped = len(skip_prs)
            # Ledger (OMN-12569): record a terminal SKIPPED conclusion for each
            # PR the reducer chose not to act on.
            for intent in skip_prs:
                self._record_ledger_event(
                    kind=EnumPrLedgerEventKind.FINAL_CONCLUSION,
                    run_id=command.run_id,
                    correlation_id=command.correlation_id,
                    repo=intent.repo,
                    pr_number=intent.pr_number,
                    conclusion=EnumPrLedgerConclusion.SKIPPED,
                    orchestrator_action=EnumOrchestratorAction.SKIP,
                    phase=state.phase,
                )

            # Phase: MERGING (merge-group checks; skip if fix_only). ``merge_prs``
            # here is the verification-cleared subset when verify=True — PRs whose
            # verification failed have already been removed and left open.
            if merge_prs and not command.fix_only:
                await self._transition_phase(
                    state,
                    EnumOrchestratorState.MERGING,
                    run_id=command.run_id,
                    correlation_id=command.correlation_id,
                )

                assert self._merge is not None
                # Real merge handler signature: handle(command: ModelPrMergeCommand)
                # Fan out: one command per PR, gated per-PR by the arm-gate
                # (OMN-14151), aggregate the results.
                merge_result = await self._call_merge_fanout(
                    correlation_id=command.correlation_id,
                    prs_to_merge=merge_prs,
                    dry_run=command.dry_run,
                    inv_result=state.inventory_result,
                    policy=ModelArmGatePolicy(
                        action_mode=command.action_mode,
                        kill_switch=command.merge_queue_mutation_kill_switch,
                        wave_cap=command.merge_wave_cap,
                        enable_stall_remediation=command.enable_stall_remediation,
                    ),
                )
                state.prs_merged = merge_result.prs_merged
                logger.info(
                    "[PR-LIFECYCLE-ORCH] merge completed: %d merged, %d failed",
                    merge_result.prs_merged,
                    merge_result.prs_failed,
                )

                # Phase: POST_MERGE_TAIL (OMN-12570). A merged PR's terminal
                # conclusion is reached after GitHub runs CI on the merge target
                # branch — a DISTINCT phase from the merge-group checks. We
                # transition explicitly so the MERGED conclusion (and any future
                # tail failure) is attributed to POST_MERGE_TAIL, not MERGING.
                await self._transition_phase(
                    state,
                    EnumOrchestratorState.POST_MERGE_TAIL,
                    run_id=command.run_id,
                    correlation_id=command.correlation_id,
                )
                # Ledger (OMN-12569): record a terminal MERGED conclusion for
                # each PR routed through merge. The synthetic merge-group SHA is
                # derived per-PR so the projection captures it as provenance;
                # the conclusion is stamped with the POST_MERGE_TAIL phase.
                for tr in merge_prs:
                    self._record_ledger_event(
                        kind=EnumPrLedgerEventKind.FINAL_CONCLUSION,
                        run_id=command.run_id,
                        correlation_id=command.correlation_id,
                        repo=tr.repo,
                        pr_number=tr.pr_number,
                        merge_group_sha=_synthetic_merge_group_sha(
                            command.run_id, tr.repo, tr.pr_number
                        ),
                        conclusion=EnumPrLedgerConclusion.MERGED,
                        orchestrator_action=EnumOrchestratorAction.MERGE,
                        phase=state.phase,
                    )

                # OMN-13859: event-driven worktree prune-on-close. The merges
                # just performed ARE the trigger; prune each merged PR's
                # worktree scoped to its (ticket, repo). Runs only when at least
                # one PR actually merged, and is fully best-effort — the prune
                # effect's rails keep dirty/canonical/out-of-root worktrees.
                if merge_result.prs_merged > 0:
                    await self._prune_merged_worktrees(
                        merged=merge_prs,
                        inventory=state.inventory_result,
                        correlation_id=command.correlation_id,
                    )

                if command.merge_only:
                    await self._transition_phase(
                        state,
                        EnumOrchestratorState.COMPLETE,
                        run_id=command.run_id,
                        correlation_id=command.correlation_id,
                    )
                    return self._build_result(state, command.correlation_id)

            # Phase: FIXING (branch checks; skip if merge_only)
            if fix_prs and not command.merge_only:
                await self._transition_phase(
                    state,
                    EnumOrchestratorState.FIXING,
                    run_id=command.run_id,
                    correlation_id=command.correlation_id,
                )
                await self._publish_fixer_dispatch_start(
                    fix_prs, command.correlation_id
                )
                # OMN-13586 RH-4: fan-out to repo-health classify/repair lane.
                # Publishes classify cmd for all fix_prs that have a
                # validation_failure_origin set; additionally publishes
                # repair-start only for REPO_BASELINE origins.
                # Best-effort — a publish error must never abort the sweep.
                await self._publish_repo_health_fanout(fix_prs, command.correlation_id)

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
                state.prs_delegated_fix_attempted = sum(
                    r.prs_delegated_fix_attempted for r in fix_results
                )
                state.prs_delegated_fix_accepted = sum(
                    r.prs_delegated_fix_accepted for r in fix_results
                )
                state.prs_delegated_fix_gate_failed = sum(
                    r.prs_delegated_fix_gate_failed for r in fix_results
                )
                state.prs_delegated_fix_escalated = sum(
                    r.prs_delegated_fix_escalated for r in fix_results
                )
                state.delegation_cost_savings_usd = sum(
                    r.delegation_cost_savings_usd for r in fix_results
                )
                logger.info(
                    "[PR-LIFECYCLE-ORCH] fix completed: %d dispatched, %d skipped, "
                    "delegated attempted=%d accepted=%d gate_failed=%d escalated=%d",
                    state.prs_fixed,
                    sum(r.prs_skipped for r in fix_results),
                    state.prs_delegated_fix_attempted,
                    state.prs_delegated_fix_accepted,
                    state.prs_delegated_fix_gate_failed,
                    state.prs_delegated_fix_escalated,
                )
                # Ledger (OMN-12569): record a terminal FAILED conclusion (a
                # non-green PR routed to remediation) with FIX action provenance.
                # Stamped with the BRANCH_CHECKS phase so this failure is
                # distinguishable from a POST_MERGE_TAIL failure (OMN-12570).
                for tr in fix_prs:
                    self._record_ledger_event(
                        kind=EnumPrLedgerEventKind.FINAL_CONCLUSION,
                        run_id=command.run_id,
                        correlation_id=command.correlation_id,
                        repo=tr.repo,
                        pr_number=tr.pr_number,
                        head_sha=None,
                        conclusion=EnumPrLedgerConclusion.FAILED,
                        orchestrator_action=EnumOrchestratorAction.FIX,
                        phase=state.phase,
                    )

            await self._transition_phase(
                state,
                EnumOrchestratorState.COMPLETE,
                run_id=command.run_id,
                correlation_id=command.correlation_id,
            )

        except Exception as exc:
            from_state = state.fsm.value
            logger.exception(
                "[PR-LIFECYCLE-ORCH] failed in phase %s: %s",
                from_state,
                exc,
            )
            state.error_message = str(exc)
            await self._transition_phase(
                state,
                EnumOrchestratorState.FAILED,
                run_id=command.run_id,
                correlation_id=command.correlation_id,
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

    def _collect_org_wide_open_prs(self) -> Any:
        """Census every open PR across the org as the sweep-done precondition.

        Delegates to the inventory handler's ``collect_org_wide_open_prs`` so the
        org-wide search (``gh api /search/issues?q=org:OmniNode-ai is:pr is:open``)
        lives in one place (OMN-13318). Overridable in tests to inject a
        synthetic census without real gh calls.

        Returns a ``ModelOrgWideOpenPrInventory`` when available, or ``None`` if
        the wired inventory handler does not expose the census (e.g. a minimal
        test double) — in which case the sweep-done gate is treated as satisfied.
        """
        self._ensure_sub_handlers()
        collector = getattr(self._inventory, "collect_org_wide_open_prs", None)
        if collector is None:
            return None
        return collector()

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
        stuck_queue_prs: list[Any] = []
        for repo in repos:
            pr_numbers = self._enumerate_open_pr_numbers(repo)
            if not pr_numbers:
                continue

            input_model = ModelPrInventoryInput(repo=repo, pr_numbers=pr_numbers)
            raw = self._inventory.handle(input_model)
            # Short-circuit: test stub returned InventoryResult directly.
            if isinstance(raw, InventoryResult):
                return raw
            stuck_queue_prs.extend(getattr(raw, "stuck_queue_prs", ()))
            # raw is ModelPrInventoryOutput; adapt to PrRecord sequence.
            for pr_state in getattr(raw, "pr_states", ()):
                title = getattr(pr_state, "title", "")
                branch = getattr(pr_state, "head_ref", "")
                all_prs.append(
                    PrRecord(
                        pr_number=pr_state.pr_number,
                        repo=pr_state.repo,
                        title=title,
                        branch=branch,
                        head_sha=getattr(pr_state, "head_sha", None),
                        ticket_ids=_extract_ticket_ids(title, branch),
                        checks_status=_map_ci_status(pr_state),
                        review_status=_map_review_status(pr_state),
                        has_conflicts=getattr(pr_state, "has_conflicts", False),
                        failed_check_names=_failed_check_names(pr_state),
                        failed_check_flaky_evidence=_failed_check_flaky_evidence(
                            pr_state
                        ),
                        merge_state_status=getattr(
                            pr_state, "merge_state_status", None
                        ),
                        # OMN-14151: genuine tri-state facts for the arm-gate.
                        # ``is_draft`` is threaded straight from the inventory
                        # read; ``coderabbit_unresolved`` is None when the
                        # inventory handler never collected it (never
                        # defaulted to 0).
                        is_draft=getattr(pr_state, "is_draft", None),
                        coderabbit_unresolved=getattr(
                            pr_state, "coderabbit_unresolved", None
                        ),
                    )
                )

        return InventoryResult(
            prs=tuple(all_prs),
            total_collected=len(all_prs),
            stuck_queue_prs=tuple(stuck_queue_prs),
        )

    def _make_merge_queue_adapter(self) -> Any:
        from omnimarket.nodes.node_pr_lifecycle_merge_effect.handlers.adapter_github_merge_queue import (
            GitHubMergeQueueAdapter,
        )

        return GitHubMergeQueueAdapter()

    async def _remediate_stalled_queue_prs(
        self,
        command: ModelPrLifecycleStartCommand,
        inv_result: InventoryResult,
    ) -> None:
        if not inv_result.stuck_queue_prs:
            return
        if command.dry_run or command.inventory_only:
            logger.info(
                "[PR-LIFECYCLE-ORCH] detected %d stalled queue PRs; "
                "remediation skipped dry_run=%s inventory_only=%s",
                len(inv_result.stuck_queue_prs),
                command.dry_run,
                command.inventory_only,
            )
            return
        # OMN-14151: stall remediation dequeues + re-enqueues an ALREADY-armed
        # PR to re-mint a stuck merge-group SHA — a separate operation from
        # readiness-arming an unarmed PR, so it is NOT routed through the
        # arm-gate. It is instead gated behind its own explicit opt-in
        # (enable_stall_remediation) plus the same action_mode/kill_switch
        # envelope, so the shipped conservative config has exactly one active
        # arm path.
        if (
            command.action_mode is not EnumArmActionMode.ENFORCE
            or command.merge_queue_mutation_kill_switch
            or not command.enable_stall_remediation
        ):
            logger.info(
                "[PR-LIFECYCLE-ORCH] detected %d stalled queue PRs; "
                "remediation withheld action_mode=%s kill_switch=%s "
                "enable_stall_remediation=%s",
                len(inv_result.stuck_queue_prs),
                command.action_mode.value,
                command.merge_queue_mutation_kill_switch,
                command.enable_stall_remediation,
            )
            return

        adapter = self._make_merge_queue_adapter()
        for entry in inv_result.stuck_queue_prs:
            queue_state = getattr(entry, "queue_state", "")
            run_count = getattr(entry, "merge_group_run_count", None)
            if queue_state != "AWAITING_CHECKS" or run_count != 0:
                continue
            repo = str(entry.repo)
            pr_number = int(entry.pr_number)
            try:
                action = await adapter.remediate_queue_stall(repo, pr_number)
                logger.warning(
                    "[PR-LIFECYCLE-ORCH] merge queue stall remediation: %s",
                    action,
                )
                # Ledger (OMN-12569): a dequeue+re-enqueue mints a fresh
                # merge-group SHA and counts as a rerun attempt. Record it with
                # the head SHA observed at stall detection for provenance.
                # OMN-12570: a re-enqueue acts on the MERGE_GROUP phase (it
                # re-mints the merge-group commit) regardless of where the
                # coarse FSM is during inventory, so attribute it explicitly.
                self._record_ledger_event(
                    kind=EnumPrLedgerEventKind.RERUN_ATTEMPTED,
                    run_id=command.run_id,
                    correlation_id=command.correlation_id,
                    repo=repo,
                    pr_number=pr_number,
                    head_sha=getattr(entry, "head_sha", None),
                    merge_group_sha=_synthetic_merge_group_sha(
                        command.run_id, repo, pr_number, attempt=2
                    ),
                    orchestrator_action=EnumOrchestratorAction.REQUEUE,
                    phase=EnumPrLifecyclePhase.MERGE_GROUP,
                )
            except Exception as exc:
                logger.warning(
                    "[PR-LIFECYCLE-ORCH] merge queue stall remediation failed "
                    "for %s#%s: %s",
                    repo,
                    pr_number,
                    exc,
                    exc_info=True,
                )

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
                ticket_ids=pr.ticket_ids,
                ci_status=_orch_checks_to_ci_status(pr.checks_status),
                has_conflicts=pr.has_conflicts,
                approved=(
                    pr.review_status == "approved"
                    or str(pr.merge_state_status or "").upper() == "CLEAN"
                ),
                failed_check_names=pr.failed_check_names,
                failed_check_flaky_evidence=pr.failed_check_flaky_evidence,
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
                    ticket_ids=getattr(result, "ticket_ids", ()),
                    failed_check_names=getattr(result, "failed_check_names", ()),
                    failed_check_flaky_evidence=getattr(
                        result, "failed_check_flaky_evidence", ()
                    ),
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

    @staticmethod
    def _status_checks_for_arm(checks_status: str) -> str | None:
        """Map the genuinely-collected orchestrator checks_status to the
        arm-gate's SUCCESS/FAILURE/PENDING vocabulary (OMN-14151).

        ``checks_status`` already reflects a positively-collected fact (PR-
        associated check runs only, per F3/OMN-13319) — this is a vocabulary
        translation, not a second inference path. "unknown" maps to None so
        the arm-gate WITHHOLDs rather than treating unknown as a pass.
        """
        mapping = {"success": "SUCCESS", "failure": "FAILURE", "pending": "PENDING"}
        return mapping.get(checks_status.lower())

    async def _evaluate_arm_gate(
        self,
        *,
        pr: TriageRecord,
        pr_record: PrRecord | None,
        policy: ModelArmGatePolicy,
    ) -> EnumArmDecision:
        """Evaluate the sole ARM/WITHHOLD decider for one merge-intent PR.

        Builds a ModelArmCandidate from genuine facts collected at inventory
        time (never re-derived here) plus a live OCC-companion read-back, and
        delegates the ARM/WITHHOLD call entirely to node_pr_arm_gate_compute.

        The OCC-companion read-back (a live gh/remote call) is skipped
        whenever the policy alone already guarantees WITHHOLD (action_mode is
        not ENFORCE, or the kill switch is engaged) — report_only is the
        default posture and must cost zero extra external calls, not just
        zero mutation.
        """
        assert self._arm_gate is not None
        occ_companion_verified: bool | None = None
        if policy.action_mode is EnumArmActionMode.ENFORCE and not policy.kill_switch:
            occ_readback = await self._occ_stamp_readback.verify_fix_landed(
                pr.repo, pr.pr_number
            )
            occ_companion_verified = occ_readback.verified
        candidate = ModelArmCandidate(
            repo=pr.repo,
            pr_number=pr.pr_number,
            is_draft=pr_record.is_draft if pr_record is not None else None,
            coderabbit_unresolved=(
                pr_record.coderabbit_unresolved if pr_record is not None else None
            ),
            merge_state_status=(
                pr_record.merge_state_status if pr_record is not None else None
            ),
            status_checks=(
                self._status_checks_for_arm(pr_record.checks_status)
                if pr_record is not None
                else None
            ),
            occ_companion_verified=occ_companion_verified,
        )
        raw_decision = await self._arm_gate.handle(
            ModelArmGateRequest(candidate=candidate, policy=policy)
        )
        # Short-circuit: a test double may return the bare EnumArmDecision.
        if isinstance(raw_decision, EnumArmDecision):
            return raw_decision
        decision: EnumArmDecision = raw_decision.decision
        return decision

    async def _call_merge_fanout(
        self,
        *,
        correlation_id: UUID,
        prs_to_merge: tuple[TriageRecord, ...],
        dry_run: bool,
        inv_result: InventoryResult | None = None,
        policy: ModelArmGatePolicy | None = None,
    ) -> MergeResult:
        """Fan out merge commands to the merge handler (one command per PR).

        Every PR is first evaluated by node_pr_arm_gate_compute (OMN-14151),
        the sole ARM/WITHHOLD decider — this is the ONE gated path that can
        mutate the merge queue. Only ARM-decided PRs, priority-ordered and
        capped at ``policy.wave_cap``, reach the merge handler; every other PR
        is a report-only no-op this pass (default posture: zero mutation).

        HandlerPrLifecycleMerge.handle(command: ModelPrMergeCommand) → ModelPrMergeResult.
        """
        assert self._merge is not None
        assert self._arm_gate is not None

        from omnimarket.nodes.node_pr_lifecycle_merge_effect.models.model_merge_command import (
            ModelPrMergeCommand,
        )

        effective_policy = policy if policy is not None else ModelArmGatePolicy()
        pr_lookup: dict[tuple[str, int], PrRecord] = {}
        if inv_result is not None:
            pr_lookup = {(p.repo, p.pr_number): p for p in inv_result.prs}

        armed: list[TriageRecord] = []
        for pr in prs_to_merge:
            decision = await self._evaluate_arm_gate(
                pr=pr,
                pr_record=pr_lookup.get((pr.repo, pr.pr_number)),
                policy=effective_policy,
            )
            if decision is EnumArmDecision.ARM:
                armed.append(pr)
            else:
                logger.info(
                    "[PR-LIFECYCLE-ORCH] arm-gate WITHHOLD %s#%s (report-only "
                    "this pass)",
                    pr.repo,
                    pr.pr_number,
                )

        # Wave-cap: bound blast radius to the first N ARM-decided PRs,
        # deterministically ordered (repo, pr_number) since this slice ships
        # with a flat priority_hint of 0 for every candidate.
        armed = sorted(armed, key=lambda pr: (pr.repo, pr.pr_number))
        capped = armed[: effective_policy.wave_cap]
        if len(armed) > len(capped):
            logger.info(
                "[PR-LIFECYCLE-ORCH] wave_cap=%d bounded %d ARM-decided PR(s) "
                "to %d this pass",
                effective_policy.wave_cap,
                len(armed),
                len(capped),
            )

        prs_merged = 0
        prs_failed = 0
        for pr in capped:
            merge_command = ModelPrMergeCommand(
                correlation_id=correlation_id,
                pr_number=pr.pr_number,
                repo=pr.repo,
                triage_verdict=pr.category.value,
                use_merge_queue=True,
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
            elif getattr(raw, "error", None):
                prs_failed += 1

        return MergeResult(prs_merged=prs_merged, prs_failed=prs_failed)

    async def _prune_merged_worktrees(
        self,
        *,
        merged: tuple[TriageRecord, ...],
        inventory: InventoryResult | None,
        correlation_id: UUID,
    ) -> PruneResult:
        """Prune the git worktree for each just-merged PR (OMN-13859).

        Event-driven prune-on-close: the merges we performed in this sweep ARE
        the trigger. One command per (ticket, repo) — scoped to exactly the
        worktrees whose PRs just closed, never a full-registry scan. The prune
        effect owns every safety rail (dirty→flag, canonical-clone→refuse,
        outside-root→refuse, no @{u} requirement).

        Best-effort: a prune failure or an unresolved worktrees root is logged
        and swallowed so worktree GC never aborts a successful merge sweep.
        """
        if self._prune is None:
            return PruneResult()

        from omnimarket.events.worktree_prune import (
            ModelWorktreePruneCommand,
        )

        # Recover branch per (repo, pr_number) from inventory for provenance/logging.
        branch_by_pr: dict[tuple[str, int], str] = {}
        if inventory is not None:
            for rec in inventory.prs:
                if rec.branch:
                    branch_by_pr[(rec.repo, rec.pr_number)] = rec.branch

        pruned = 0
        flagged_dirty = 0
        skipped = 0
        for tr in merged:
            branch = branch_by_pr.get((tr.repo, tr.pr_number))
            for ticket_id in tr.ticket_ids:
                try:
                    command = ModelWorktreePruneCommand(
                        correlation_id=correlation_id,
                        ticket_id=ticket_id,
                        repo=tr.repo,
                        branch=branch,
                        pr_number=tr.pr_number,
                    )
                    result = await self._prune.handle(command)
                except Exception as exc:
                    logger.warning(
                        "[PR-LIFECYCLE-ORCH] worktree prune raised for "
                        "ticket=%s repo=%s pr=%s: %s",
                        ticket_id,
                        tr.repo,
                        tr.pr_number,
                        exc,
                    )
                    skipped += 1
                    continue
                outcome = getattr(result, "outcome", None)
                outcome_value = getattr(outcome, "value", outcome)
                if outcome_value == "pruned":
                    pruned += 1
                elif outcome_value == "skipped_dirty":
                    flagged_dirty += 1
                else:
                    skipped += 1

        if pruned or flagged_dirty:
            logger.info(
                "[PR-LIFECYCLE-ORCH] worktree prune tail: %d pruned, "
                "%d flagged-dirty (kept), %d skipped",
                pruned,
                flagged_dirty,
                skipped,
            )
        return PruneResult(
            worktrees_pruned=pruned,
            worktrees_flagged_dirty=flagged_dirty,
            worktrees_skipped=skipped,
        )

    # -----------------------------------------------------------------------
    # VERIFYING phase (OMN-13673 / OMN-7742): per-PR pre-merge verification gate
    # -----------------------------------------------------------------------

    async def _run_verification(
        self,
        *,
        merge_prs: tuple[TriageRecord, ...],
        command: ModelPrLifecycleStartCommand,
        state: _SweepState,
    ) -> tuple[TriageRecord, ...]:
        """Run the per-PR pre-merge verification gate and return cleared PRs.

        For each merge-ready PR the verification target is computed from the
        PR's changed files (``verify_target_mapping``), the canonical
        verification probe for that target is dispatched, and the per-PR
        outcome is classified into one of the 7 OMN-7742/OMN-8390 categories.

        Blocking semantics (OMN-13673 + OMN-13831 fail-closed):
          * ``VERIFICATION_FAILED`` always blocks the PR — it is excluded from
            the returned set and left open.
          * An *indeterminate* outcome (``VERIFICATION_UNAVAILABLE`` /
            ``VERIFICATION_TIMEOUT`` / ``VERIFICATION_TOOL_ERROR``) blocks the PR
            when its mapped verification target indicates code files
            (``target != SKIPPED_NO_MAPPING``) OR when the PR's changed files
            could not be enumerated at all. Indeterminate verification of code
            must fail CLOSED — a transient probe/gh failure must never let a code
            PR merge unverified (OMN-13831).
          * NEUTRAL (still merges): a successful ``MERGED`` verification, a
            genuine docs-only / no-mapping PR (``SKIPPED_NO_MAPPING``), an
            indeterminate outcome on a docs-only / no-mapping PR, and
            ``SKIPPED_BY_POLICY`` (dry_run).

        Blocked PRs record a terminal FAILED ledger conclusion in the
        BRANCH_CHECKS phase so the durable ledger reflects that they did not
        merge for verification reasons.

        Per-PR isolation: a probe error (or an un-enumerable changed-file list)
        for one PR affects only that PR and never aborts the batch.

        In ``dry_run`` the gate classifies every PR as ``SKIPPED_BY_POLICY``
        without executing real probes, so the 7-category breakdown is still
        materialized for evidence.
        """
        cleared: list[TriageRecord] = []
        for pr in merge_prs:
            target = EnumVerificationTarget.SKIPPED_NO_MAPPING
            changed_files_unavailable = False
            try:
                target = self._verification_target_for(pr.repo, pr.pr_number)
                if command.dry_run:
                    outcome = EnumVerificationOutcome.SKIPPED_BY_POLICY
                elif target == EnumVerificationTarget.SKIPPED_NO_MAPPING:
                    outcome = EnumVerificationOutcome.SKIPPED_NO_MAPPING
                else:
                    outcome = await self._execute_verification_probe(
                        target=target,
                        timeout_seconds=command.verify_timeout_seconds,
                    )
            except ChangedFilesUnavailableError as exc:
                # Fail CLOSED (OMN-13831): the PR's changed files could not be
                # enumerated even after a retry, so we cannot prove it is
                # docs-only. Treat it as an indeterminate code PR and block below.
                logger.warning(
                    "[PR-LIFECYCLE-ORCH] changed files unavailable for "
                    "repo=%s pr=%d — failing closed (not merging): %s",
                    pr.repo,
                    pr.pr_number,
                    exc,
                )
                changed_files_unavailable = True
                outcome = EnumVerificationOutcome.VERIFICATION_UNAVAILABLE
            except Exception as exc:
                # Per-PR isolation — a probe failure for one PR is an
                # indeterminate tool error for that PR only, never an abort of
                # the whole batch. Whether it BLOCKS depends on the target below.
                logger.exception(
                    "[PR-LIFECYCLE-ORCH] verification probe raised for "
                    "repo=%s pr=%d target=%s: %s",
                    pr.repo,
                    pr.pr_number,
                    target,
                    exc,
                )
                outcome = EnumVerificationOutcome.VERIFICATION_TOOL_ERROR

            state.verification_outcomes[(pr.repo, pr.pr_number)] = outcome.value
            await self._publish_verification_completed(
                pr=pr,
                target=target,
                outcome=outcome,
                correlation_id=command.correlation_id,
            )
            logger.info(
                "[PR-LIFECYCLE-ORCH] verification %s repo=%s pr=%d target=%s",
                outcome.value,
                pr.repo,
                pr.pr_number,
                target.value,
            )

            # Fail-closed blocking decision (OMN-13673 + OMN-13831).
            is_code_pr = (
                changed_files_unavailable
                or target != EnumVerificationTarget.SKIPPED_NO_MAPPING
            )
            should_block = outcome == EnumVerificationOutcome.VERIFICATION_FAILED or (
                outcome in _INDETERMINATE_VERIFICATION_OUTCOMES and is_code_pr
            )

            if should_block:
                # Blocked: the PR stays open. Record a terminal FAILED ledger
                # conclusion in the BRANCH_CHECKS phase (state.phase == VERIFYING
                # maps to BRANCH_CHECKS) so the durable ledger reflects that it
                # did not merge for verification reasons.
                state.prs_verification_blocked += 1
                self._record_ledger_event(
                    kind=EnumPrLedgerEventKind.FINAL_CONCLUSION,
                    run_id=command.run_id,
                    correlation_id=command.correlation_id,
                    repo=pr.repo,
                    pr_number=pr.pr_number,
                    conclusion=EnumPrLedgerConclusion.FAILED,
                    orchestrator_action=EnumOrchestratorAction.FIX,
                    phase=state.phase,
                )
                continue

            if outcome == EnumVerificationOutcome.MERGED:
                state.prs_verified += 1
            cleared.append(pr)

        logger.info(
            "[PR-LIFECYCLE-ORCH] verification complete: %d cleared, %d blocked "
            "(breakdown=%s)",
            len(cleared),
            state.prs_verification_blocked,
            _render_verification_breakdown(state.verification_outcomes.values()),
        )
        return tuple(cleared)

    def _verification_target_for(
        self,
        repo: str,
        pr_number: int,
    ) -> EnumVerificationTarget:
        """Map a PR's changed files to its verification target (OMN-7742)."""
        changed_files = self._pr_changed_files(repo, pr_number)
        return map_changed_files_to_target(changed_files)

    def _pr_changed_files(self, repo: str, pr_number: int) -> list[str]:
        """Return a PR's changed file paths via the gh CLI.

        Overridable in tests (or subclasses) to avoid real network calls.

        Returns an empty list ONLY when ``gh`` succeeds and the PR genuinely has
        zero changed paths (→ SKIPPED_NO_MAPPING, a neutral skip). On a ``gh``
        failure the call is retried once; if it still fails this raises
        ``ChangedFilesUnavailableError`` rather than returning ``[]`` (OMN-13831).
        Silently returning ``[]`` on error would map a transient ``gh`` outage to
        a neutral no-mapping skip and let a code PR merge unverified — so the
        indeterminate case must be surfaced and fail CLOSED upstream.
        """
        import subprocess

        attempts = 2
        last_error = "<unknown>"
        for attempt in range(1, attempts + 1):
            try:
                proc = subprocess.run(
                    [
                        "gh",
                        "pr",
                        "view",
                        str(pr_number),
                        "--repo",
                        repo,
                        "--json",
                        "files",
                        "--jq",
                        ".files[].path",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "[PR-LIFECYCLE-ORCH] gh pr view files raised for %s#%d "
                    "(attempt %d/%d): %s",
                    repo,
                    pr_number,
                    attempt,
                    attempts,
                    exc,
                )
                continue

            if proc.returncode != 0:
                last_error = proc.stderr.strip() or f"returncode={proc.returncode}"
                logger.warning(
                    "[PR-LIFECYCLE-ORCH] gh pr view files failed for %s#%d "
                    "(attempt %d/%d, returncode=%d): %s",
                    repo,
                    pr_number,
                    attempt,
                    attempts,
                    proc.returncode,
                    proc.stderr.strip() or "<no stderr>",
                )
                continue

            # Success: an empty stdout here is a GENUINE zero-changed-files PR.
            return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

        raise ChangedFilesUnavailableError(
            f"gh pr view files failed for {repo}#{pr_number} after "
            f"{attempts} attempts: {last_error}"
        )

    async def _execute_verification_probe(
        self,
        *,
        target: EnumVerificationTarget,
        timeout_seconds: int,
    ) -> EnumVerificationOutcome:
        """Dispatch the canonical verification probe for a target.

        Overridable in tests to inject outcomes without live Docker / nodes.

        * ``RUNTIME_HEALTH`` → the canonical local runtime-health probe
          (``probe_runtime_health`` from ``verify_target_mapping``). ``total_failed
          == 0`` is a pass (MERGED); any failure is VERIFICATION_FAILED.
        * Every other recognized target → dispatch the canonical verification
          EFFECT node (``node_verify_effect``). ``all_critical_passed`` is a pass;
          a critical failure is VERIFICATION_FAILED. A timeout maps to
          VERIFICATION_TIMEOUT, an unwired node to VERIFICATION_UNAVAILABLE, and
          any other error to VERIFICATION_TOOL_ERROR — all NEUTRAL (non-blocking).
        """
        if target == EnumVerificationTarget.RUNTIME_HEALTH:
            try:
                report = await asyncio.to_thread(probe_runtime_health)
            except Exception as exc:
                logger.warning(
                    "[PR-LIFECYCLE-ORCH] runtime-health probe error: %s", exc
                )
                return EnumVerificationOutcome.VERIFICATION_TOOL_ERROR
            return (
                EnumVerificationOutcome.MERGED
                if report.total_failed == 0
                else EnumVerificationOutcome.VERIFICATION_FAILED
            )

        try:
            from omnimarket.nodes.node_verify_effect.handlers.handler_verify import (
                HandlerVerify,
            )
        except ImportError:
            logger.warning(
                "[PR-LIFECYCLE-ORCH] canonical verify node unavailable for "
                "target=%s — neutral skip",
                target.value,
            )
            return EnumVerificationOutcome.VERIFICATION_UNAVAILABLE

        verify_handler = HandlerVerify()
        try:
            result = await asyncio.wait_for(
                verify_handler.handle(correlation_id=uuid4()),
                timeout=timeout_seconds,
            )
        except TimeoutError:
            return EnumVerificationOutcome.VERIFICATION_TIMEOUT
        except Exception as exc:
            logger.warning(
                "[PR-LIFECYCLE-ORCH] canonical verify node error target=%s: %s",
                target.value,
                exc,
            )
            return EnumVerificationOutcome.VERIFICATION_TOOL_ERROR
        return (
            EnumVerificationOutcome.MERGED
            if getattr(result, "all_critical_passed", False)
            else EnumVerificationOutcome.VERIFICATION_FAILED
        )

    async def _publish_verification_completed(
        self,
        *,
        pr: TriageRecord,
        target: EnumVerificationTarget,
        outcome: EnumVerificationOutcome,
        correlation_id: UUID,
    ) -> None:
        """Publish a per-PR verification-completed event (bus-native).

        Best-effort — a publish error must never abort the sweep.
        """
        payload = json.dumps(
            {
                "pr_number": pr.pr_number,
                "repo": pr.repo,
                "target": target.value,
                "outcome": outcome.value,
                "correlation_id": str(correlation_id),
            }
        ).encode()
        try:
            await self._event_bus.publish(
                topic=self._topic_verification_completed,
                key=None,
                value=payload,
            )
        except Exception as exc:
            logger.warning(
                "[PR-LIFECYCLE-ORCH] failed to publish verification-completed "
                "pr=%d repo=%s: %s",
                pr.pr_number,
                pr.repo,
                exc,
            )

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
                    ModelPrLifecycleFixCommand,
                )

                fix_command = ModelPrLifecycleFixCommand(
                    correlation_id=correlation_id,
                    pr_number=pr.pr_number,
                    repo=pr.repo,
                    block_reason=_block_reason_for_fix(pr),
                    ticket_id=pr.ticket_ids[0] if pr.ticket_ids else None,
                    dry_run=dry_run,
                    requested_at=datetime.now(tz=UTC),
                )
                assert self._fix is not None
                raw = await self._fix.handle(fix_command)
                # Map ModelPrLifecycleFixResult → FixResult aggregate.
                if isinstance(raw, FixResult):
                    return raw
                fix_applied: bool = getattr(raw, "fix_applied", False)
                # OMN-14191 (generalizes OMN-14173; closes OMN-14174): never count
                # prs_fixed on DISPATCH. Every fix arm — not just the OCC-autobind
                # arm — is gated on an INDEPENDENT read-back over live gh/remote
                # state: the orchestrator re-reads the ACTUAL pushed OCC companion
                # PR + the product PR body (Piece-2 canonical parser) and counts
                # the fix ONLY when the OCC stamp is confirmed landed. This never
                # trusts the fix handler's self-reported fix_applied /
                # occ_companion_verified flag (CLAUDE.md Rule 3). Arms that legit-
                # imately land no OCC stamp (a bare CI rerun, a dispatched-but-not-
                # yet-landed polish) fail-closed to NOT counted — the safe under-
                # count direction OMN-14174 requires ("do not treat dispatch as
                # success"). dry_run lands nothing, so it is never counted and
                # never hits the network.
                counted_as_fixed = False
                if fix_applied and not dry_run:
                    readback = await self._occ_stamp_readback.verify_fix_landed(
                        pr.repo, pr.pr_number, fix_command.ticket_id
                    )
                    counted_as_fixed = readback.verified
                    logger.info(
                        "[PR-LIFECYCLE-ORCH] fix read-back pr=%s repo=%s "
                        "reason=%s counted=%s",
                        pr.pr_number,
                        pr.repo,
                        readback.reason,
                        counted_as_fixed,
                    )
                delegation_outcome = getattr(raw, "delegation_outcome", None)
                delegation_outcome_value = (
                    getattr(delegation_outcome, "value", delegation_outcome)
                    if delegation_outcome is not None
                    else None
                )
                # `is True` (not just truthy) so a MagicMock/duck-typed test
                # double without an explicit `delegated` attribute never
                # spuriously counts as an attempted delegation.
                delegated: bool = getattr(raw, "delegated", False) is True
                return FixResult(
                    prs_dispatched=1 if counted_as_fixed else 0,
                    prs_skipped=0 if counted_as_fixed else 1,
                    prs_delegated_fix_attempted=1 if delegated else 0,
                    prs_delegated_fix_accepted=(
                        1 if delegation_outcome_value == "accepted" else 0
                    ),
                    prs_delegated_fix_gate_failed=(
                        1 if delegation_outcome_value == "gate_failed" else 0
                    ),
                    prs_delegated_fix_escalated=(
                        1 if delegation_outcome_value == "escalated" else 0
                    ),
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

    def _occ_dependency_edges(
        self,
        *,
        triage_result: PrTriageResult,
        reducer_result: ReducerResult,
        occ_merge_sha: str,
    ) -> tuple[OccDependencyEdge, ...]:
        """Build dependency edges for receipt-only failures.

        ``ticket_id`` is the primary join key. PR numbers are secondary
        references because a downstream PR can move or be recreated while the
        OCC evidence ticket identity remains stable.
        """
        skipped_occ = {
            (intent.repo, intent.pr_number)
            for intent in reducer_result.intents
            if intent.intent == EnumReducerIntent.SKIP
        }
        edges: list[OccDependencyEdge] = []
        for pr in triage_result.classified:
            if pr.category != EnumPrCategory.OCC_DEPENDENCY:
                continue
            if (pr.repo, pr.pr_number) not in skipped_occ:
                continue
            for ticket_id in pr.ticket_ids:
                rerun_guard_key = (
                    f"{ticket_id}:{pr.repo}#{pr.pr_number}:occ:{occ_merge_sha}"
                )
                edges.append(
                    OccDependencyEdge(
                        ticket_id=ticket_id,
                        downstream_repo=pr.repo,
                        downstream_pr_number=pr.pr_number,
                        downstream_failed_check_names=pr.failed_check_names,
                        reason=pr.block_reason,
                        rerun_guard_key=rerun_guard_key,
                    )
                )
        return tuple(edges)

    @staticmethod
    def _resolve_occ_merge_sha(
        *,
        triage_result: PrTriageResult,
        reducer_result: ReducerResult,
    ) -> str:
        """Return the OCC evidence revision for rerun guard keys when available."""
        for source in (reducer_result, triage_result):
            value = getattr(source, "occ_merge_sha", None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        env_value = os.environ.get("ONEX_OCC_MERGE_SHA", "")  # contract-config-ok: config  # fmt: skip
        if env_value.strip():
            return env_value.strip()
        return _UNKNOWN_OCC_MERGE_SHA

    def _build_result(
        self, state: _SweepState, correlation_id: UUID
    ) -> ModelPrLifecycleResult:
        final_state, remainders = self._apply_org_wide_done_gate(state)
        return ModelPrLifecycleResult(
            correlation_id=correlation_id,
            prs_inventoried=state.prs_inventoried,
            prs_merged=state.prs_merged,
            prs_fixed=state.prs_fixed,
            prs_skipped=state.prs_skipped,
            prs_verified=state.prs_verified,
            prs_verification_blocked=state.prs_verification_blocked,
            verification_breakdown=_render_verification_breakdown(
                state.verification_outcomes.values()
            ),
            final_state=final_state,
            error_message=state.error_message,
            org_wide_open_count=int(getattr(state.org_wide_open, "open_count", 0) or 0),
            org_wide_open_remainders=remainders,
            prs_delegated_fix_attempted=state.prs_delegated_fix_attempted,
            prs_delegated_fix_accepted=state.prs_delegated_fix_accepted,
            prs_delegated_fix_gate_failed=state.prs_delegated_fix_gate_failed,
            prs_delegated_fix_escalated=state.prs_delegated_fix_escalated,
            delegation_cost_savings_usd=state.delegation_cost_savings_usd,
        )

    @staticmethod
    def _apply_org_wide_done_gate(
        state: _SweepState,
    ) -> tuple[str, tuple[OrgWideOpenPrRemainderRef, ...]]:
        """Gate the reported final_state on the org-wide open-PR census (OMN-13318).

        A sweep may only report COMPLETE when zero PRs remain open org-wide. If
        the FSM reached COMPLETE but the census reports open PRs (or could not be
        executed), the reported state is downgraded to NOT_DONE and the open-PR
        remainders are surfaced so the sweep-done report is refused with the
        exact PRs that still block it.

        A FAILED sweep is left untouched — its failure already blocks the report.
        When no census is available (e.g. a minimal test double that does not
        expose ``collect_org_wide_open_prs``), the gate is a no-op.
        """
        if state.fsm is not EnumOrchestratorState.COMPLETE:
            return state.fsm.value, ()

        census = state.org_wide_open
        if census is None:
            return state.fsm.value, ()

        if getattr(census, "sweep_done", True):
            return state.fsm.value, ()

        remainders = tuple(
            OrgWideOpenPrRemainderRef(
                repo=str(getattr(item, "repo", "")),
                pr_number=int(getattr(item, "pr_number", 0) or 0),
                title=str(getattr(item, "title", "") or ""),
                url=str(getattr(item, "url", "") or ""),
            )
            for item in getattr(census, "remainders", ()) or ()
        )
        logger.warning(
            "[PR-LIFECYCLE-ORCH] sweep-done REFUSED: %d org-wide open PR(s) "
            "remain (query_failed=%s)",
            int(getattr(census, "open_count", 0) or 0),
            getattr(census, "query_failed", False),
        )
        return _FINAL_STATE_NOT_DONE, remainders

    def _sweep_run_dir(self, run_id: str) -> Path | None:
        state_dir = self._resolved_state_dir()
        base = (Path(state_dir) / "merge-sweep").resolve()
        out_dir = (base / run_id).resolve()
        if not out_dir.is_relative_to(base):
            logger.error(
                "[PR-LIFECYCLE-ORCH] refusing to write sweep artifact: run_id "
                "escapes merge-sweep root run_id=%s resolved=%s base=%s",
                run_id,
                out_dir,
                base,
            )
            return None
        return out_dir

    @staticmethod
    def _resolved_state_dir() -> Path:
        """Resolve a writable state root and reject root-anchored fallbacks."""
        raw_state_dir = os.environ.get("ONEX_STATE_DIR")
        if raw_state_dir:
            candidate = Path(raw_state_dir).expanduser()
            resolved = candidate.resolve()
            if resolved not in {Path("/"), Path("/.onex_state")}:
                return resolved

        omni_home = os.environ.get("OMNI_HOME")
        if omni_home:
            return (Path(omni_home).expanduser() / ".onex_state").resolve()
        return Path(os.path.expanduser("~/.onex_state")).resolve()

    def _write_occ_dependency_edges_file(
        self,
        run_id: str,
        edges: tuple[OccDependencyEdge, ...],
    ) -> None:
        """Persist OCC dependency edges for downstream rerun automation."""
        if not edges:
            return
        out_dir = self._sweep_run_dir(run_id)
        if out_dir is None:
            return
        out_path = out_dir / "occ_dependency_edges.json"
        payload = {
            "run_id": run_id,
            "primary_identity": "ticket_id",
            "rerun_policy": (
                "rerun at most once per OCC merge SHA per downstream PR, "
                "and only for previous OCC_DEPENDENCY classifications"
            ),
            "edges": [edge.model_dump(mode="json") for edge in edges],
        }
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(payload, indent=2))
            logger.info(
                "[PR-LIFECYCLE-ORCH] wrote OCC dependency edges run_id=%s path=%s",
                run_id,
                out_path,
            )
        except Exception as exc:
            logger.warning(
                "[PR-LIFECYCLE-ORCH] failed to write OCC dependency edges "
                "run_id=%s path=%s: %s",
                run_id,
                out_path,
                exc,
            )

    def _write_result_file(self, run_id: str, result: ModelPrLifecycleResult) -> None:
        """Persist the orchestrator result as ModelSkillResult-shaped JSON.

        The merge_sweep skill polls ``$ONEX_STATE_DIR/merge-sweep/{run_id}/result.json``.
        A missing ``$ONEX_STATE_DIR`` falls back to ``~/.onex_state`` so that local
        test runs still produce a file.
        """
        out_dir = self._sweep_run_dir(run_id)
        if out_dir is None:
            return
        out_path = out_dir / "result.json"

        is_failure = result.final_state == EnumOrchestratorState.FAILED.value
        # OMN-13318: NOT_DONE is not a success — open PRs remain org-wide. The
        # sweep-done report is refused; report it as a distinct not_done status
        # carrying the blocking remainders.
        is_not_done = result.final_state == _FINAL_STATE_NOT_DONE
        if is_failure:
            status = "error"
        elif is_not_done:
            status = "not_done"
        else:
            status = "success"
        payload: dict[str, Any] = {
            "skill_name": "merge-sweep",
            "status": status,
            "run_id": run_id,
            "correlation_id": str(result.correlation_id),
            "final_state": result.final_state,
            "prs_inventoried": result.prs_inventoried,
            "prs_merged": result.prs_merged,
            "prs_fixed": result.prs_fixed,
            "prs_skipped": result.prs_skipped,
            "prs_verified": result.prs_verified,
            "prs_verification_blocked": result.prs_verification_blocked,
            "verification_breakdown": result.verification_breakdown,
            "prs_delegated_fix_attempted": result.prs_delegated_fix_attempted,
            "prs_delegated_fix_accepted": result.prs_delegated_fix_accepted,
            "prs_delegated_fix_gate_failed": result.prs_delegated_fix_gate_failed,
            "prs_delegated_fix_escalated": result.prs_delegated_fix_escalated,
            "delegation_cost_savings_usd": result.delegation_cost_savings_usd,
            "org_wide_open_count": result.org_wide_open_count,
            "org_wide_open_remainders": [
                remainder.model_dump(mode="json")
                for remainder in result.org_wide_open_remainders
            ],
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
        if not self._topic_fixer_dispatch_start:
            return
        for pr in fix_prs:
            # OMN-13987 CP2: the fixer dispatcher routes on machine
            # EnumStallCategory literals, not on the human-readable block_reason
            # prose. Emit the machine category so RED→node_ci_fix_effect and
            # CONFLICTED→node_conflict_hunk_effect actually route. block_reason
            # prose stays as the advisory ``blocking_reason`` for humans/logs.
            payload = {
                "pr_number": pr.pr_number,
                "repo": pr.repo,
                "stall_category": _stall_category_for_dispatch(pr.category),
                "blocking_reason": pr.block_reason or "",
                "correlation_id": str(correlation_id),
            }
            envelope: ModelEventEnvelope[dict[str, object]] = ModelEventEnvelope(
                payload=payload,
                correlation_id=correlation_id,
                source_tool="pr_lifecycle_orchestrator",
                target_tool="node_fixer_dispatcher",
                event_type=EVENT_TYPE_FIXER_DISPATCH_START,
            )
            encoded = json.dumps(
                {
                    **envelope.model_dump(mode="json"),
                    "event_type": EVENT_TYPE_FIXER_DISPATCH_START,
                }
            ).encode()
            await self._event_bus.publish(
                topic=self._topic_fixer_dispatch_start,
                key=None,
                value=encoded,
            )

    async def _publish_repo_health_fanout(
        self,
        fix_prs: tuple[TriageRecord, ...],
        correlation_id: UUID,
    ) -> None:
        """RH-4 fan-out: publish repo-health classify/repair commands.

        For each fix_pr with a ``validation_failure_origin`` set:
          - Publish ``onex.cmd.omnimarket.repo-health-classify.v1`` (all non-None
            origins) so node_repo_health_classify_compute can record the origin.
          - Additionally publish ``onex.cmd.omnimarket.repo-health-repair-start.v1``
            only when origin is REPO_BASELINE (the repair lane is triggered).

        Decision rules per plan §3.3 (OMN-13586):
          - repo_baseline  → classify cmd + repair-start cmd
          - pr_scoped      → classify cmd only (stays in existing fix lane)
          - unknown        → classify cmd only (surface evidence, no auto repair)
          - external_dependency → classify cmd only (surfaced, no code repair task)
          - None           → no commands (no validation failure observed)

        Guardrail: these publishes are best-effort notifications — a publish
        failure must never abort a sweep. Repo-baseline debt must NOT become a
        new hard block on auto-merge arming (plan facts #9/#10).

        Related: OMN-13586 RH-4, OMN-13316 epic.
        """
        for pr in fix_prs:
            origin = pr.validation_failure_origin
            if origin is None:
                continue

            # Emit the classify command for all non-None origins.
            classify_payload = json.dumps(
                {
                    "pr_number": pr.pr_number,
                    "repo": pr.repo,
                    "failure_origin": origin.value,
                    "block_reason": pr.block_reason or "",
                    "failed_check_names": list(pr.failed_check_names),
                    "correlation_id": str(correlation_id),
                }
            ).encode()
            try:
                await self._event_bus.publish(
                    topic=TOPIC_REPO_HEALTH_CLASSIFY,
                    key=None,
                    value=classify_payload,
                )
            except Exception as exc:
                logger.warning(
                    "[PR-LIFECYCLE-ORCH] failed to publish repo-health-classify "
                    "pr=%s repo=%s origin=%s: %s",
                    pr.pr_number,
                    pr.repo,
                    origin,
                    exc,
                )

            # Emit repair-start only for repo_baseline — pr_scoped and unknown
            # do not trigger an automated repair task.
            if origin is not EnumFailureOrigin.REPO_BASELINE:
                continue

            repair_payload = json.dumps(
                {
                    "pr_number": pr.pr_number,
                    "repo": pr.repo,
                    "failure_origin": origin.value,
                    "block_reason": pr.block_reason or "",
                    "failed_check_names": list(pr.failed_check_names),
                    "correlation_id": str(correlation_id),
                }
            ).encode()
            try:
                await self._event_bus.publish(
                    topic=TOPIC_REPO_HEALTH_REPAIR_START,
                    key=None,
                    value=repair_payload,
                )
            except Exception as exc:
                logger.warning(
                    "[PR-LIFECYCLE-ORCH] failed to publish repo-health-repair-start "
                    "pr=%s repo=%s: %s",
                    pr.pr_number,
                    pr.repo,
                    exc,
                )


__all__: list[str] = [
    "HandlerPrLifecycleOrchestrator",
    "ModelPrLifecycleResult",
    "ModelPrLifecycleStartCommand",
]
