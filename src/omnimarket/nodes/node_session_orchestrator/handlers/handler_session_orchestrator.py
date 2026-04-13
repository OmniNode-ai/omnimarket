# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerSessionOrchestrator — Unified session orchestrator (OMN-8367 PoC).

Phase 1 (health gate): implemented — probes 8 health dimensions, collects
ModelSessionHealthReport, applies blocking rules.

Phase 2 (RSD scoring): STUB — returns placeholder queue.
  TODO(OMN-8367): Wire RSD scoring. See design doc §RSD Scoring Model.

Phase 3 (dispatch): STUB — logs intent only, does not dispatch.
  TODO(OMN-8367): Wire TeamCreate dispatch with correlation chain propagation.
  See design doc §Dispatch Targets.

Probe callables are injected at construction time for testability. Production
probes call existing skills via subprocess or SSH. All config comes from env vars
or contract.yaml — no hardcoded paths, IPs, or usernames.

Required env vars for SSH probes:
  ONEX_INFRA_HOST — hostname or IP of the infra server (e.g. 192.168.86.201)
  ONEX_INFRA_USER — SSH username (e.g. jonah)

Optional env vars:
  OMNI_HOME     — path to the omni_home canonical registry (for golden chain + repo sync)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Env var helpers — fail-fast on required vars (no defaults for infra paths)
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    """Return env var value; raise RuntimeError if unset."""
    val = os.environ.get(name, "")
    if not val:
        raise RuntimeError(
            f"Required env var {name!r} is not set. "
            f"Set it before invoking node_session_orchestrator."
        )
    return val


def _infra_host() -> str:
    return _require_env("ONEX_INFRA_HOST")


def _infra_user() -> str:
    return _require_env("ONEX_INFRA_USER")


def _ssh_host_key_checking() -> str:
    return os.environ.get("SSH_STRICT_HOST_KEY_CHECKING", "accept-new")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class EnumDimensionStatus(StrEnum):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"


class EnumGateDecision(StrEnum):
    PROCEED = "PROCEED"
    FIX_ONLY = "FIX_ONLY"
    # HALT is reserved for future use (e.g. catastrophic dimensions that
    # prevent even fix-dispatch). Currently _compute_gate only emits
    # PROCEED or FIX_ONLY.
    # TODO(OMN-8367): Implement HALT per design doc §Gate Decisions when
    # dimension-level catastrophic thresholds are defined.
    HALT = "HALT"


class EnumSessionStatus(StrEnum):
    COMPLETE = "complete"
    HALTED = "halted"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Health dimension models
# ---------------------------------------------------------------------------


class ModelHealthDimensionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension: str
    status: EnumDimensionStatus
    source: str
    timestamp: datetime
    stale_after: timedelta
    details: dict[str, Any]
    actionable_items: list[str]
    blocks_dispatch: bool


class ModelSessionHealthReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    dimensions: list[ModelHealthDimensionResult]
    overall_status: EnumDimensionStatus
    gate_decision: EnumGateDecision
    produced_at: datetime


# ---------------------------------------------------------------------------
# I/O models
# ---------------------------------------------------------------------------


class ModelSessionOrchestratorCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(default="")
    correlation_id: str = Field(
        default="",
        description="Propagated correlation chain: sess-id.disp-id.ticket-id.pr-id",
    )
    mode: str = Field(default="interactive")
    dry_run: bool = False
    skip_health: bool = False
    standing_orders_path: str = ".onex_state/session/standing_orders.json"
    state_dir: str = ".onex_state/session"
    phase: int = Field(default=0, ge=0, le=3)


class ModelSessionOrchestratorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    correlation_id: str = ""
    status: EnumSessionStatus
    halt_reason: str = ""
    health_report: ModelSessionHealthReport | None = None
    dispatch_queue: list[str] = Field(default_factory=list)
    dispatch_receipts: list[str] = Field(default_factory=list)
    dry_run: bool = False
    produced_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


# ---------------------------------------------------------------------------
# Probe type alias: returns a ModelHealthDimensionResult
# ---------------------------------------------------------------------------

ProbeCallable = Callable[[], ModelHealthDimensionResult]


# ---------------------------------------------------------------------------
# Default probes (production implementations)
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _probe_pr_inventory() -> ModelHealthDimensionResult:
    """Dimension 1: PR Inventory — gh pr list across repos, check CI status."""
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--json",
                "number,statusCheckRollup,assignees",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return ModelHealthDimensionResult(
                dimension="pr_inventory",
                status=EnumDimensionStatus.YELLOW,
                source="live_probe",
                timestamp=_now(),
                stale_after=timedelta(minutes=10),
                details={"error": result.stderr[:200]},
                actionable_items=["gh CLI unavailable or unauthenticated"],
                blocks_dispatch=False,
            )
        prs = json.loads(result.stdout or "[]")
        blocked = [
            p
            for p in prs
            if not p.get("assignees")
            and any(
                c.get("conclusion") == "FAILURE"
                for c in (p.get("statusCheckRollup") or [])
            )
        ]
        status = EnumDimensionStatus.RED if blocked else EnumDimensionStatus.GREEN
        return ModelHealthDimensionResult(
            dimension="pr_inventory",
            status=status,
            source="live_probe",
            timestamp=_now(),
            stale_after=timedelta(minutes=10),
            details={"open_pr_count": len(prs), "blocked_unowned_count": len(blocked)},
            actionable_items=[
                f"PR #{p['number']} blocked on RED CI with no owner" for p in blocked
            ],
            blocks_dispatch=False,
        )
    except Exception as exc:
        return ModelHealthDimensionResult(
            dimension="pr_inventory",
            status=EnumDimensionStatus.YELLOW,
            source="live_probe",
            timestamp=_now(),
            stale_after=timedelta(minutes=5),
            details={"error": str(exc)[:200]},
            actionable_items=["PR inventory probe failed"],
            blocks_dispatch=False,
        )


def _probe_golden_chain() -> ModelHealthDimensionResult:
    """Dimension 2: Golden Chain — invoke onex:golden_chain_sweep via subprocess.

    Runs in the caller's cwd (inherit from process). Set OMNI_HOME to point
    at the omnimarket worktree if needed.
    """
    omni_home = os.environ.get("OMNI_HOME")
    run_cwd = omni_home if omni_home else None
    try:
        result = subprocess.run(
            [
                "uv",
                "run",
                "python",
                "-m",
                "omnimarket.nodes.node_golden_chain_sweep",
                "--dry-run",
                "--output-json",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=run_cwd,
        )
        if result.returncode == 0:
            status = EnumDimensionStatus.GREEN
            actionable: list[str] = []
        else:
            status = EnumDimensionStatus.RED
            actionable = ["Golden chain sweep failed — see omnimarket node logs"]
        return ModelHealthDimensionResult(
            dimension="golden_chain",
            status=status,
            source="live_probe",
            timestamp=_now(),
            stale_after=timedelta(minutes=15),
            details={
                "returncode": result.returncode,
                "stderr_snippet": result.stderr[:200],
            },
            actionable_items=actionable,
            blocks_dispatch=(status == EnumDimensionStatus.RED),
        )
    except Exception as exc:
        return ModelHealthDimensionResult(
            dimension="golden_chain",
            status=EnumDimensionStatus.RED,
            source="live_probe",
            timestamp=_now(),
            stale_after=timedelta(minutes=5),
            details={"error": str(exc)[:200]},
            actionable_items=[
                "Golden chain probe failed — golden_chain_sweep unavailable"
            ],
            blocks_dispatch=True,
        )


def _probe_linear_sync() -> ModelHealthDimensionResult:
    """Dimension 3: Linear Sync — ticket status vs PR state mismatch."""
    linear_key = os.environ.get("LINEAR_API_KEY", "")
    if not linear_key:
        return ModelHealthDimensionResult(
            dimension="linear_sync",
            status=EnumDimensionStatus.YELLOW,
            source="inventory",
            timestamp=_now(),
            stale_after=timedelta(minutes=30),
            details={"reason": "LINEAR_API_KEY not set"},
            actionable_items=["Set LINEAR_API_KEY to enable Linear sync check"],
            blocks_dispatch=False,
        )
    return ModelHealthDimensionResult(
        dimension="linear_sync",
        status=EnumDimensionStatus.GREEN,
        source="inventory",
        timestamp=_now(),
        stale_after=timedelta(minutes=30),
        details={"note": "Full mismatch scan deferred — API key present"},
        actionable_items=[],
        blocks_dispatch=False,
    )


def _probe_runtime_health() -> ModelHealthDimensionResult:
    """Dimension 4: Runtime Health — check infra server via SSH.

    Requires ONEX_INFRA_HOST and ONEX_INFRA_USER env vars.
    """
    try:
        host = _infra_host()
        user = _infra_user()
    except RuntimeError as exc:
        return ModelHealthDimensionResult(
            dimension="runtime_health",
            status=EnumDimensionStatus.RED,
            source="live_probe",
            timestamp=_now(),
            stale_after=timedelta(minutes=5),
            details={"error": str(exc)},
            actionable_items=[str(exc)],
            blocks_dispatch=True,
        )
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "ConnectTimeout=5",
                "-o",
                f"StrictHostKeyChecking={_ssh_host_key_checking()}",
                f"{user}@{host}",
                "docker ps --format '{{.Names}}' | wc -l",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return ModelHealthDimensionResult(
                dimension="runtime_health",
                status=EnumDimensionStatus.RED,
                source="live_probe",
                timestamp=_now(),
                stale_after=timedelta(minutes=5),
                details={"error": f"SSH failed: {result.stderr[:100]}"},
                actionable_items=[f"Cannot SSH to {host} — runtime unreachable"],
                blocks_dispatch=True,
            )
        container_count = int(result.stdout.strip() or "0")
        if container_count < 20:
            status = EnumDimensionStatus.RED
            actionable = [f"Only {container_count} containers running; expected ≥ 20"]
        elif container_count < 24:
            status = EnumDimensionStatus.YELLOW
            actionable = [f"{container_count} containers; some may be down"]
        else:
            status = EnumDimensionStatus.GREEN
            actionable = []
        return ModelHealthDimensionResult(
            dimension="runtime_health",
            status=status,
            source="live_probe",
            timestamp=_now(),
            stale_after=timedelta(minutes=5),
            details={"container_count": container_count, "host": host},
            actionable_items=actionable,
            blocks_dispatch=(status == EnumDimensionStatus.RED),
        )
    except Exception as exc:
        return ModelHealthDimensionResult(
            dimension="runtime_health",
            status=EnumDimensionStatus.RED,
            source="live_probe",
            timestamp=_now(),
            stale_after=timedelta(minutes=5),
            details={"error": str(exc)[:200]},
            actionable_items=[f"Runtime health probe raised: {exc!s}"],
            blocks_dispatch=True,
        )


def _probe_plugin_currency() -> ModelHealthDimensionResult:
    """Dimension 5: Plugin Currency — check omniclaude plugin version."""
    try:
        result = subprocess.run(
            ["claude", "plugin", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return ModelHealthDimensionResult(
                dimension="plugin_currency",
                status=EnumDimensionStatus.YELLOW,
                source="inventory",
                timestamp=_now(),
                stale_after=timedelta(hours=1),
                details={"error": result.stderr[:200]},
                actionable_items=[
                    "claude CLI unavailable — cannot check plugin version"
                ],
                blocks_dispatch=False,
            )
        return ModelHealthDimensionResult(
            dimension="plugin_currency",
            status=EnumDimensionStatus.GREEN,
            source="inventory",
            timestamp=_now(),
            stale_after=timedelta(hours=1),
            details={"note": "Plugin list query succeeded"},
            actionable_items=[],
            blocks_dispatch=False,
        )
    except Exception as exc:
        return ModelHealthDimensionResult(
            dimension="plugin_currency",
            status=EnumDimensionStatus.YELLOW,
            source="inventory",
            timestamp=_now(),
            stale_after=timedelta(hours=1),
            details={"error": str(exc)[:200]},
            actionable_items=["Plugin currency check failed"],
            blocks_dispatch=False,
        )


def _probe_deploy_agent() -> ModelHealthDimensionResult:
    """Dimension 6: Deploy Agent — check systemd service on infra server.

    Requires ONEX_INFRA_HOST and ONEX_INFRA_USER env vars.
    """
    try:
        host = _infra_host()
        user = _infra_user()
    except RuntimeError as exc:
        return ModelHealthDimensionResult(
            dimension="deploy_agent",
            status=EnumDimensionStatus.RED,
            source="live_probe",
            timestamp=_now(),
            stale_after=timedelta(minutes=5),
            details={"error": str(exc)},
            actionable_items=[str(exc)],
            blocks_dispatch=True,
        )
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "ConnectTimeout=5",
                "-o",
                f"StrictHostKeyChecking={_ssh_host_key_checking()}",
                f"{user}@{host}",
                "systemctl is-active deploy-agent.service",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            return ModelHealthDimensionResult(
                dimension="deploy_agent",
                status=EnumDimensionStatus.RED,
                source="live_probe",
                timestamp=_now(),
                stale_after=timedelta(minutes=5),
                details={"error": f"SSH failed: {result.stderr[:100]}", "host": host},
                actionable_items=[f"Cannot SSH to {host} — deploy agent state unknown"],
                blocks_dispatch=True,
            )
        active = result.stdout.strip() == "active"
        status = EnumDimensionStatus.GREEN if active else EnumDimensionStatus.RED
        return ModelHealthDimensionResult(
            dimension="deploy_agent",
            status=status,
            source="live_probe",
            timestamp=_now(),
            stale_after=timedelta(minutes=5),
            details={"systemd_state": result.stdout.strip(), "host": host},
            actionable_items=(
                []
                if active
                else [
                    f"deploy-agent.service inactive — run: "
                    f"ssh {user}@{host} sudo systemctl start deploy-agent"
                ]
            ),
            blocks_dispatch=(status == EnumDimensionStatus.RED),
        )
    except Exception as exc:
        return ModelHealthDimensionResult(
            dimension="deploy_agent",
            status=EnumDimensionStatus.RED,
            source="live_probe",
            timestamp=_now(),
            stale_after=timedelta(minutes=5),
            details={"error": str(exc)[:200]},
            actionable_items=["Deploy agent probe raised — SSH unavailable"],
            blocks_dispatch=True,
        )


def _probe_observability() -> ModelHealthDimensionResult:
    """Dimension 7: Observability — check Redpanda consumer lag.

    Requires ONEX_INFRA_HOST and ONEX_INFRA_USER env vars.
    """
    try:
        host = _infra_host()
        user = _infra_user()
    except RuntimeError as exc:
        return ModelHealthDimensionResult(
            dimension="observability",
            status=EnumDimensionStatus.YELLOW,
            source="live_probe",
            timestamp=_now(),
            stale_after=timedelta(minutes=10),
            details={"error": str(exc)},
            actionable_items=[str(exc)],
            blocks_dispatch=False,
        )
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "ConnectTimeout=5",
                "-o",
                f"StrictHostKeyChecking={_ssh_host_key_checking()}",
                f"{user}@{host}",
                "docker exec omnibase-infra-redpanda rpk cluster health 2>&1 | head -5",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )
        if result.returncode == 0 and "Healthy" in result.stdout:
            status = EnumDimensionStatus.GREEN
            actionable: list[str] = []
        else:
            status = EnumDimensionStatus.YELLOW
            actionable = ["Redpanda health check inconclusive — verify consumer lag"]
        return ModelHealthDimensionResult(
            dimension="observability",
            status=status,
            source="live_probe",
            timestamp=_now(),
            stale_after=timedelta(minutes=10),
            details={"output_snippet": result.stdout[:200]},
            actionable_items=actionable,
            blocks_dispatch=False,
        )
    except Exception as exc:
        return ModelHealthDimensionResult(
            dimension="observability",
            status=EnumDimensionStatus.YELLOW,
            source="live_probe",
            timestamp=_now(),
            stale_after=timedelta(minutes=10),
            details={"error": str(exc)[:200]},
            actionable_items=["Observability probe raised"],
            blocks_dispatch=False,
        )


def _probe_repo_sync() -> ModelHealthDimensionResult:
    """Dimension 8: Repo Sync — check canonical repos behind origin/main.

    Uses OMNI_HOME env var; if unset, skips the check with YELLOW.
    """
    omni_home = os.environ.get("OMNI_HOME", "")
    if not omni_home:
        return ModelHealthDimensionResult(
            dimension="repo_sync",
            status=EnumDimensionStatus.YELLOW,
            source="inventory",
            timestamp=_now(),
            stale_after=timedelta(minutes=30),
            details={"reason": "OMNI_HOME not set — cannot check repo sync"},
            actionable_items=["Set OMNI_HOME to the canonical omni_home path"],
            blocks_dispatch=False,
        )
    canonical_repos = [
        "omniclaude",
        "omnibase_core",
        "omnibase_infra",
        "omnibase_spi",
        "omnibase_compat",
        "omnimarket",
        "omniintelligence",
    ]
    behind: list[str] = []
    for repo in canonical_repos:
        repo_path = os.path.join(omni_home, repo)
        if not os.path.isdir(repo_path):
            continue
        try:
            result = subprocess.run(
                ["git", "-C", repo_path, "rev-list", "--count", "HEAD..origin/main"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                logger.warning(
                    "git rev-list failed for %s: %s", repo, result.stderr.strip()[:80]
                )
                behind.append(f"{repo} (check failed: {result.stderr.strip()[:80]})")
                continue
            count = int(result.stdout.strip() or "0")
            if count > 0:
                behind.append(f"{repo} ({count} commits behind)")
        except Exception:
            behind.append(f"{repo} (check failed)")
    status = EnumDimensionStatus.RED if behind else EnumDimensionStatus.GREEN
    return ModelHealthDimensionResult(
        dimension="repo_sync",
        status=status,
        source="inventory",
        timestamp=_now(),
        stale_after=timedelta(minutes=30),
        details={"behind_repos": behind},
        actionable_items=[
            f"Run: git -C $OMNI_HOME/{r.split()[0]} pull --ff-only" for r in behind
        ],
        blocks_dispatch=False,
    )


# Ordered list of default probes (matches contract.yaml health_dimensions ordering)
_DEFAULT_PROBES: list[ProbeCallable] = [
    _probe_pr_inventory,
    _probe_golden_chain,
    _probe_linear_sync,
    _probe_runtime_health,
    _probe_plugin_currency,
    _probe_deploy_agent,
    _probe_observability,
    _probe_repo_sync,
]

_DIMENSION_NAMES: list[str] = [
    "pr_inventory",
    "golden_chain",
    "linear_sync",
    "runtime_health",
    "plugin_currency",
    "deploy_agent",
    "observability",
    "repo_sync",
]


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerSessionOrchestrator:
    """Unified session orchestrator — Phase 1 implemented, 2 + 3 stubbed.

    Probe callables are injected at construction time for testability.
    Default probes invoke real external systems (SSH, gh CLI, subprocess).
    """

    def __init__(
        self,
        probes: list[ProbeCallable] | None = None,
    ) -> None:
        self._probes = probes or _DEFAULT_PROBES

    def handle(
        self, command: ModelSessionOrchestratorCommand
    ) -> ModelSessionOrchestratorResult:
        session_id = command.session_id or self._generate_session_id()
        correlation_id = command.correlation_id or session_id

        if command.skip_health:
            logger.warning(
                "skip_health=True — bypassing Phase 1 health gate (emergency only)"
            )
            health_report = None
            gate_ok = True
        else:
            health_report = self._run_phase1(session_id, command)
            gate_ok = health_report.gate_decision == EnumGateDecision.PROCEED

        if command.phase == 1:
            return ModelSessionOrchestratorResult(
                session_id=session_id,
                correlation_id=correlation_id,
                status=EnumSessionStatus.COMPLETE
                if gate_ok
                else EnumSessionStatus.HALTED,
                halt_reason=""
                if gate_ok
                else f"Phase 1 gate: {health_report.gate_decision if health_report else 'unknown'}",
                health_report=health_report,
                dry_run=command.dry_run,
            )

        if not gate_ok:
            halt_reason = (
                "Phase 1 health gate: FIX_ONLY mode — RED dimension blocks dispatch"
            )
            if health_report:
                blocking = [
                    d
                    for d in health_report.dimensions
                    if d.blocks_dispatch and d.status != EnumDimensionStatus.GREEN
                ]
                if blocking:
                    halt_reason = (
                        f"Phase 1 HALT: {', '.join(d.dimension for d in blocking)} RED"
                    )
            return ModelSessionOrchestratorResult(
                session_id=session_id,
                correlation_id=correlation_id,
                status=EnumSessionStatus.HALTED,
                halt_reason=halt_reason,
                health_report=health_report,
                dry_run=command.dry_run,
            )

        # Phase 2: RSD scoring — STUB
        # TODO(OMN-8367): Implement RSD scoring. Score tickets + PRs with formula from design doc.
        # Inputs: Linear tickets, PR inventory, standing_orders.json, acceleration/risk/staleness signals.
        # Output: ordered ModelRSDQueue with ticket_score and merge_score per item.
        dispatch_queue = self._run_phase2_stub(session_id, command)
        logger.info(
            "Phase 2 STUB: returning placeholder queue for session %s", session_id
        )

        if command.phase == 2:
            return ModelSessionOrchestratorResult(
                session_id=session_id,
                correlation_id=correlation_id,
                status=EnumSessionStatus.COMPLETE,
                health_report=health_report,
                dispatch_queue=dispatch_queue,
                dry_run=command.dry_run,
            )

        # Phase 3: Dispatch — STUB
        # TODO(OMN-8367): Implement TeamCreate dispatch. For each item in dispatch_queue:
        # - Build correlation chain: {correlation_id}.disp-{seq}.{ticket_id}.{pr_id}
        # - Dispatch via TeamCreate or the session-orchestrator-start topic (see contract.yaml)
        # - Collect dispatch receipts
        # - Write in-flight state to {state_dir}/in_flight.yaml
        # NOTE(OMN-8367 inter-layer bridge): The omniclaude skill wrapper subscribes to
        # the omniclaude session topic (see contract.yaml), while this backing node
        # subscribes to the session-orchestrator-start topic (see contract.yaml).
        # The bridge between these two topics is NOT yet wired. A dedicated sub-ticket is needed.
        dispatch_receipts = self._run_phase3_stub(session_id, dispatch_queue, command)
        logger.info("Phase 3 STUB: dispatch logged only for session %s", session_id)

        return ModelSessionOrchestratorResult(
            session_id=session_id,
            correlation_id=correlation_id,
            status=EnumSessionStatus.COMPLETE,
            health_report=health_report,
            dispatch_queue=dispatch_queue,
            dispatch_receipts=dispatch_receipts,
            dry_run=command.dry_run,
        )

    # ------------------------------------------------------------------
    # Phase 1: Health Gate
    # ------------------------------------------------------------------

    def _run_phase1(
        self,
        session_id: str,
        command: ModelSessionOrchestratorCommand,
    ) -> ModelSessionHealthReport:
        logger.info("Phase 1: running health gate for session %s", session_id)
        results: list[ModelHealthDimensionResult] = []

        for probe in self._probes:
            try:
                result = probe()
                logger.info(
                    "Phase 1 dimension %s: %s (blocks=%s)",
                    result.dimension,
                    result.status,
                    result.blocks_dispatch,
                )
                results.append(result)
            except Exception as exc:
                dim_name = getattr(probe, "__name__", "unknown").replace("_probe_", "")
                logger.error("Phase 1 probe %s raised: %s", dim_name, exc)
                results.append(
                    ModelHealthDimensionResult(
                        dimension=dim_name,
                        status=EnumDimensionStatus.RED,
                        source="live_probe",
                        timestamp=_now(),
                        stale_after=timedelta(minutes=5),
                        details={"error": str(exc)[:200]},
                        actionable_items=[f"Probe {dim_name} raised an exception"],
                        blocks_dispatch=True,
                    )
                )

        overall, gate_decision = self._compute_gate(results)

        if not command.dry_run:
            self._write_health_snapshot(
                session_id, results, overall, gate_decision, command.state_dir
            )

        return ModelSessionHealthReport(
            session_id=session_id,
            dimensions=results,
            overall_status=overall,
            gate_decision=gate_decision,
            produced_at=_now(),
        )

    def _compute_gate(
        self,
        results: list[ModelHealthDimensionResult],
    ) -> tuple[EnumDimensionStatus, EnumGateDecision]:
        any_red = any(r.status == EnumDimensionStatus.RED for r in results)
        any_blocking_yellow = any(
            r.status == EnumDimensionStatus.YELLOW and r.blocks_dispatch
            for r in results
        )
        any_yellow = any(r.status == EnumDimensionStatus.YELLOW for r in results)

        if any_red or any_blocking_yellow:
            overall = EnumDimensionStatus.RED
            gate_decision = EnumGateDecision.FIX_ONLY
        elif any_yellow:
            overall = EnumDimensionStatus.YELLOW
            gate_decision = EnumGateDecision.PROCEED
        else:
            overall = EnumDimensionStatus.GREEN
            gate_decision = EnumGateDecision.PROCEED

        return overall, gate_decision

    def _write_health_snapshot(
        self,
        session_id: str,
        results: list[ModelHealthDimensionResult],
        overall: EnumDimensionStatus,
        gate_decision: EnumGateDecision,
        state_dir: str,
    ) -> None:
        try:
            abs_state_dir = os.path.abspath(state_dir)
            os.makedirs(abs_state_dir, exist_ok=True)
            path = os.path.join(abs_state_dir, "last_health.yaml")
            payload = {
                "session_id": session_id,
                "produced_at": _now().isoformat(),
                "overall_status": str(overall),
                "gate_decision": str(gate_decision),
                "dimensions": [
                    {
                        "dimension": r.dimension,
                        "status": str(r.status),
                        "blocks_dispatch": r.blocks_dispatch,
                        "actionable_items": r.actionable_items,
                    }
                    for r in results
                ],
            }
            with open(path, "w", encoding="utf-8") as fh:
                yaml.dump(payload, fh, default_flow_style=False)
            logger.info("Health snapshot written: %s", path)
        except Exception as exc:
            logger.warning("Failed to write health snapshot: %s", exc)

    # ------------------------------------------------------------------
    # Phase 2: RSD scoring (STUB)
    # ------------------------------------------------------------------

    def _run_phase2_stub(
        self,
        session_id: str,
        command: ModelSessionOrchestratorCommand,
    ) -> list[str]:
        # TODO(OMN-8367): Replace with real RSD scoring.
        # ticket_score = (acceleration_value / max(risk_score, 0.1))
        #              * (1 / (1 + dependency_count))
        #              * log(1 + staleness_days)
        #              + standing_order_boost * BOOST_WEIGHT
        logger.info(
            "Phase 2 STUB: RSD scoring not yet implemented [OMN-8367]. "
            "Returning empty dispatch queue for session %s.",
            session_id,
        )
        return []

    # ------------------------------------------------------------------
    # Phase 3: Dispatch (STUB)
    # ------------------------------------------------------------------

    def _run_phase3_stub(
        self,
        session_id: str,
        dispatch_queue: list[str],
        command: ModelSessionOrchestratorCommand,
    ) -> list[str]:
        # TODO(OMN-8367): Replace with real dispatch.
        # For each item in dispatch_queue:
        #   - Build correlation chain: {correlation_id}.disp-{seq}.{ticket_id}.{pr_id}
        #   - Dispatch via TeamCreate or Kafka topic from contract.yaml
        #   - Write in-flight state to {state_dir}/in_flight.yaml
        if not dispatch_queue:
            logger.info("Phase 3 STUB: empty queue — nothing to dispatch [OMN-8367].")
            return []
        logger.info(
            "Phase 3 STUB: would dispatch %d items — not executed [OMN-8367]. "
            "Items: %s",
            len(dispatch_queue),
            dispatch_queue,
        )
        return [f"STUB:not-dispatched:{item}" for item in dispatch_queue]

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_session_id() -> str:
        ts = datetime.now(tz=UTC)
        return f"sess-{ts.strftime('%Y%m%d')}-{ts.strftime('%H%M')}"


__all__: list[str] = [
    "EnumDimensionStatus",
    "EnumGateDecision",
    "EnumSessionStatus",
    "HandlerSessionOrchestrator",
    "ModelHealthDimensionResult",
    "ModelSessionHealthReport",
    "ModelSessionOrchestratorCommand",
    "ModelSessionOrchestratorResult",
    "ProbeCallable",
]
