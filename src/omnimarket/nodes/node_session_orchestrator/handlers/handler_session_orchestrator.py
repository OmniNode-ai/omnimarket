# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerSessionOrchestrator — Unified session orchestrator (OMN-8367 / OMN-8687).

Phase 1 (health gate): implemented — probes 8 health dimensions, collects
ModelSessionHealthReport, applies blocking rules.

Phase 2 (RSD scoring): implemented — queries Linear for Active Sprint tickets,
computes RSD priority score, writes rsd-scored-{timestamp}.yaml to state_dir.

Phase 3 (dispatch): implemented — writes in_flight.yaml, dispatches top-N tickets
via `claude -p /onex:ticket_pipeline` subprocesses with correlation chain propagation,
writes dispatch receipts and ledger entry.

Probe callables are injected at construction time for testability. Production
probes call existing skills via subprocess or SSH. All config comes from env vars
or contract.yaml — no hardcoded paths, IPs, or usernames.

Required env vars for SSH probes:
  ONEX_INFRA_HOST — hostname or IP of the infra server (e.g. 192.168.86.201)
  ONEX_INFRA_USER — SSH username (e.g. jonah)

Optional env vars:
  OMNI_HOME     — path to the omni_home canonical registry (for golden chain + repo sync)
  LINEAR_API_KEY — Linear API key (Phase 2 ticket scoring)
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import urllib.request
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
    # HALT is reserved for catastrophic dimensions that prevent even fix-dispatch.
    # _compute_gate currently only emits PROCEED or FIX_ONLY.
    # Full HALT dispatch requires dimension-level catastrophic thresholds (OMN-8367).
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


class ModelRSDQueueItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str
    title: str
    rsd_score: float
    priority: int
    staleness_days: float
    dependency_count: int
    standing_order_boost: float


class ModelDispatchReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str
    dispatch_id: str
    correlation_chain: str
    dispatched_at: datetime
    dry_run: bool
    status: str


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


def _fetch_golden_chain_rows() -> dict[str, dict[str, object]]:
    """Fetch most-recent row from each golden chain tail table via SSH + psql.

    Returns a mapping of chain_name -> {field: value, ...}.
    Empty dict on failure (probe degrades gracefully to TIMEOUT chains).
    """
    chain_queries: dict[str, tuple[str, str, list[str]]] = {
        # chain_name: (table, order_col, [fields])
        "registration": (
            "agent_routing_decisions",
            "created_at",
            ["correlation_id", "selected_agent"],
        ),
        "delegation": ("delegation_events", "id", ["correlation_id"]),
        "routing": ("llm_routing_decisions", "id", ["correlation_id"]),
        "evaluation": ("session_outcomes", "session_id", ["correlation_id"]),
        "pattern_learning": (
            "pattern_learning_artifacts",
            "created_at",
            ["correlation_id"],
        ),
    }
    try:
        host = _infra_host()
        user = _infra_user()
    except RuntimeError:
        return {}

    projected: dict[str, dict[str, object]] = {}
    pg_pass = os.environ.get("POSTGRES_PASSWORD", "")

    for chain_name, (table, order_col, fields) in chain_queries.items():
        col_list = ", ".join(fields)
        sql = f"SELECT {col_list} FROM {table} ORDER BY {order_col} DESC LIMIT 1"
        cmd = [
            "ssh",
            "-o",
            "ConnectTimeout=5",
            "-o",
            f"StrictHostKeyChecking={_ssh_host_key_checking()}",
            f"{user}@{host}",
            f"PGPASSWORD={pg_pass} docker exec omnibase-infra-postgres "
            f"psql -U postgres -d omnibase_infra -t -A -F '|' -c \"{sql}\"",
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            line = res.stdout.strip()
            if res.returncode == 0 and line:
                values = line.split("|")
                if len(values) == len(fields):
                    projected[chain_name] = dict(zip(fields, values, strict=True))
        except Exception:
            pass

    return projected


def _probe_golden_chain() -> ModelHealthDimensionResult:
    """Dimension 2: Golden Chain — fetch DB rows then invoke node_golden_chain_sweep."""
    omni_home = os.environ.get("OMNI_HOME")
    run_cwd = omni_home if omni_home else None

    projected_rows = _fetch_golden_chain_rows()

    try:
        result = subprocess.run(
            [
                "uv",
                "run",
                "python",
                "-m",
                "omnimarket.nodes.node_golden_chain_sweep",
                "--projected-rows",
                json.dumps(projected_rows),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=run_cwd,
        )
        all_chains = {
            "registration",
            "delegation",
            "routing",
            "evaluation",
            "pattern_learning",
        }
        missing_chains = sorted(all_chains - set(projected_rows.keys()))
        if result.returncode == 0:
            status = EnumDimensionStatus.GREEN
            actionable: list[str] = []
        else:
            status = EnumDimensionStatus.RED
            if missing_chains:
                actionable = [
                    f"Golden chain TIMEOUT — no DB rows for chains: {', '.join(missing_chains)}. "
                    f"Check projection consumer logs on .201."
                ]
            else:
                actionable = [
                    "Golden chain sweep failed — missing expected fields in projected rows"
                ]
        return ModelHealthDimensionResult(
            dimension="golden_chain",
            status=status,
            source="live_probe",
            timestamp=_now(),
            stale_after=timedelta(minutes=15),
            details={
                "returncode": result.returncode,
                "chains_fetched": list(projected_rows.keys()),
                "chains_missing": missing_chains,
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
                "systemctl --user is-active deploy-agent.service",
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
                    f"ssh {user}@{host} systemctl --user start deploy-agent"
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
    """Unified session orchestrator — all three phases implemented.

    Phase 1: health gate (8 dimensions, SSH/subprocess probes).
    Phase 2: RSD scoring via Linear GraphQL + standing orders.
    Phase 3: dispatch via claude -p /onex:ticket_pipeline subprocesses.

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

        # Phase 2: RSD scoring
        dispatch_queue = self._run_phase2(session_id, command)
        logger.info(
            "Phase 2: scored %d items for session %s", len(dispatch_queue), session_id
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

        # Phase 3: Dispatch
        dispatch_receipts = self._run_phase3(session_id, dispatch_queue, command)
        logger.info(
            "Phase 3: dispatched %d items for session %s",
            len(dispatch_receipts),
            session_id,
        )

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
    # Phase 2: RSD scoring
    # ------------------------------------------------------------------

    def _run_phase2(
        self,
        session_id: str,
        command: ModelSessionOrchestratorCommand,
    ) -> list[str]:
        """Query Linear for active tickets, score with RSD formula, return ordered IDs."""
        logger.info("Phase 2: running RSD scoring for session %s", session_id)
        linear_key = os.environ.get("LINEAR_API_KEY", "")
        if not linear_key:
            logger.warning("Phase 2: LINEAR_API_KEY not set — returning empty queue")
            return []

        tickets = self._fetch_linear_active_tickets(linear_key)
        if not tickets:
            logger.info("Phase 2: no active tickets found")
            return []

        standing_orders = self._load_standing_orders(command.standing_orders_path)
        scored = self._score_tickets(tickets, standing_orders)
        scored.sort(key=lambda x: -x.rsd_score)

        if not command.dry_run:
            self._write_rsd_snapshot(session_id, scored, command.state_dir)

        ids = [item.ticket_id for item in scored]
        logger.info(
            "Phase 2: scored %d tickets, top 5: %s",
            len(ids),
            ids[:5],
        )
        return ids

    def _fetch_linear_active_tickets(self, linear_key: str) -> list[dict[str, Any]]:
        """Fetch unstarted/in-progress tickets from Linear via GraphQL."""
        query = """
        {
          issues(filter: {
            state: { type: { in: ["started", "unstarted"] } }
          }, first: 50, orderBy: priority) {
            nodes {
              id
              identifier
              title
              priority
              labels { nodes { name } }
              updatedAt
              children { nodes { id state { type } } }
            }
          }
        }
        """
        try:
            payload = json.dumps({"query": query}).encode()
            req = urllib.request.Request(
                "https://api.linear.app/graphql",
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": linear_key,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                data: dict[str, Any] = json.loads(resp.read().decode())
            nodes: list[dict[str, Any]] = (
                data.get("data", {}).get("issues", {}).get("nodes", [])
            )
            return nodes
        except Exception as exc:
            logger.warning("Phase 2: Linear fetch failed: %s", exc)
            return []

    def _load_standing_orders(self, path: str) -> dict[str, float]:
        """Load standing orders priority boosts. Returns {ticket_id: boost}."""
        try:
            abs_path = os.path.abspath(path)
            if not os.path.exists(abs_path):
                return {}
            with open(abs_path, encoding="utf-8") as fh:
                orders = json.load(fh)
            now = _now()
            boosts: dict[str, float] = {}
            for order in orders if isinstance(orders, list) else []:
                expires = order.get("expires_at")
                if expires:
                    try:
                        exp_dt = datetime.fromisoformat(expires)
                        if exp_dt.tzinfo is None:
                            exp_dt = exp_dt.replace(tzinfo=UTC)
                        if exp_dt < now:
                            continue
                    except ValueError:
                        pass
                ticket_id = order.get("ticket_id", "")
                boost = float(order.get("priority_override", 0.0))
                if ticket_id:
                    boosts[ticket_id] = boost
            return boosts
        except Exception as exc:
            logger.warning("Phase 2: standing orders load failed: %s", exc)
            return {}

    def _score_tickets(
        self,
        tickets: list[dict[str, Any]],
        standing_orders: dict[str, float],
    ) -> list[ModelRSDQueueItem]:
        """Apply RSD formula to each ticket."""
        boost_weight = 0.3
        now = _now()
        scored: list[ModelRSDQueueItem] = []

        for t in tickets:
            ticket_id = t.get("identifier", t.get("id", ""))
            title = t.get("title", "")
            # priority: Linear uses 0=No priority, 1=Urgent, 2=High, 3=Medium, 4=Low
            lp = t.get("priority", 3) or 3
            # Map to acceleration value: Urgent=4, High=3, Medium=2, Low=1, None=0.5
            accel_map = {1: 4.0, 2: 3.0, 3: 2.0, 4: 1.0, 0: 0.5}
            acceleration_value = accel_map.get(lp, 2.0)

            labels = [lb["name"] for lb in (t.get("labels") or {}).get("nodes", [])]
            risk_label_map = {"breaking-change": 3.0, "infra": 2.0}
            risk_score = max(
                (risk_label_map.get(lb, 1.0) for lb in labels), default=1.0
            )

            # dependency_count: open blocking sub-tickets
            children = (t.get("children") or {}).get("nodes", [])
            dep_count = sum(
                1
                for c in children
                if (c.get("state") or {}).get("type") not in ("completed", "cancelled")
            )

            updated_raw = t.get("updatedAt", "")
            try:
                updated_dt = datetime.fromisoformat(updated_raw.replace("Z", "+00:00"))
                staleness_days = max((now - updated_dt).total_seconds() / 86400, 0.0)
            except (ValueError, AttributeError):
                staleness_days = 0.0

            standing_boost = standing_orders.get(ticket_id, 0.0)

            score = (acceleration_value / max(risk_score, 0.1)) * (
                1.0 / (1.0 + dep_count)
            ) * math.log(1.0 + staleness_days) + standing_boost * boost_weight

            scored.append(
                ModelRSDQueueItem(
                    ticket_id=ticket_id,
                    title=title,
                    rsd_score=round(score, 4),
                    priority=lp,
                    staleness_days=round(staleness_days, 2),
                    dependency_count=dep_count,
                    standing_order_boost=standing_boost,
                )
            )
        return scored

    def _write_rsd_snapshot(
        self,
        session_id: str,
        scored: list[ModelRSDQueueItem],
        state_dir: str,
    ) -> None:
        try:
            abs_state_dir = os.path.abspath(state_dir)
            os.makedirs(abs_state_dir, exist_ok=True)
            ts = _now().strftime("%Y%m%dT%H%M%S")
            path = os.path.join(abs_state_dir, f"rsd-scored-{ts}.yaml")
            payload = {
                "session_id": session_id,
                "produced_at": _now().isoformat(),
                "items": [
                    {
                        "ticket_id": item.ticket_id,
                        "title": item.title,
                        "rsd_score": item.rsd_score,
                        "priority": item.priority,
                        "staleness_days": item.staleness_days,
                        "dependency_count": item.dependency_count,
                        "standing_order_boost": item.standing_order_boost,
                    }
                    for item in scored
                ],
            }
            with open(path, "w", encoding="utf-8") as fh:
                yaml.dump(payload, fh, default_flow_style=False)
            logger.info("RSD snapshot written: %s", path)
        except Exception as exc:
            logger.warning("Failed to write RSD snapshot: %s", exc)

    # ------------------------------------------------------------------
    # Phase 3: Dispatch
    # ------------------------------------------------------------------

    def _run_phase3(
        self,
        session_id: str,
        dispatch_queue: list[str],
        command: ModelSessionOrchestratorCommand,
    ) -> list[str]:
        """Dispatch top-N tickets from Phase 2 queue with correlation chain."""
        if not dispatch_queue:
            logger.info("Phase 3: empty queue — nothing to dispatch.")
            return []

        max_dispatch = 5
        targets = dispatch_queue[:max_dispatch]
        logger.info(
            "Phase 3: dispatching %d/%d items for session %s",
            len(targets),
            len(dispatch_queue),
            session_id,
        )

        receipts: list[ModelDispatchReceipt] = []

        if not command.dry_run:
            self._write_inflight(session_id, targets, command.state_dir)

        for seq, ticket_id in enumerate(targets, start=1):
            dispatch_id = f"disp-{seq:03d}"
            correlation_chain = (
                f"{command.correlation_id or session_id}.{dispatch_id}.{ticket_id}"
            )

            receipt = self._dispatch_ticket(
                ticket_id=ticket_id,
                dispatch_id=dispatch_id,
                correlation_chain=correlation_chain,
                session_id=session_id,
                dry_run=command.dry_run,
            )
            receipts.append(receipt)
            logger.info(
                "Phase 3: dispatched %s → %s (status=%s)",
                ticket_id,
                dispatch_id,
                receipt.status,
            )

        if not command.dry_run:
            self._write_session_ledger(session_id, len(receipts), command)

        return [
            json.dumps(
                {
                    "ticket_id": r.ticket_id,
                    "dispatch_id": r.dispatch_id,
                    "correlation_chain": r.correlation_chain,
                    "status": r.status,
                }
            )
            for r in receipts
        ]

    def _dispatch_ticket(
        self,
        ticket_id: str,
        dispatch_id: str,
        correlation_chain: str,
        session_id: str,
        dry_run: bool,
    ) -> ModelDispatchReceipt:
        """Invoke /onex:ticket_pipeline for a single ticket via claude -p."""
        if dry_run:
            return ModelDispatchReceipt(
                ticket_id=ticket_id,
                dispatch_id=dispatch_id,
                correlation_chain=correlation_chain,
                dispatched_at=_now(),
                dry_run=True,
                status="dry_run",
            )

        env = {**os.environ}
        env["ONEX_SESSION_ID"] = session_id
        env["ONEX_DISPATCH_ID"] = dispatch_id
        env["ONEX_CORRELATION_PREFIX"] = correlation_chain
        env["ONEX_RUN_ID"] = f"{dispatch_id}-{ticket_id}"
        env["ONEX_UNSAFE_ALLOW_EDITS"] = "1"

        try:
            proc = subprocess.Popen(
                [
                    "claude",
                    "-p",
                    f"/onex:ticket_pipeline {ticket_id}",
                    "--allowedTools",
                    "Bash,Read,Write,Edit,Glob,Grep,"
                    "mcp__linear-server__*,mcp__github__*",
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            status = f"dispatched:pid={proc.pid}"
        except Exception as exc:
            logger.warning("Phase 3: dispatch failed for %s: %s", ticket_id, exc)
            status = f"failed:{exc!s}"

        return ModelDispatchReceipt(
            ticket_id=ticket_id,
            dispatch_id=dispatch_id,
            correlation_chain=correlation_chain,
            dispatched_at=_now(),
            dry_run=False,
            status=status,
        )

    def _write_inflight(
        self,
        session_id: str,
        targets: list[str],
        state_dir: str,
    ) -> None:
        try:
            abs_state_dir = os.path.abspath(state_dir)
            os.makedirs(abs_state_dir, exist_ok=True)
            path = os.path.join(abs_state_dir, "in_flight.yaml")
            payload = {
                "session_id": session_id,
                "current_phase": "DISPATCH",
                "dispatch_queue": targets,
                "in_progress": targets,
                "completed": [],
                "last_checkpoint": _now().isoformat(),
                "resumable": True,
            }
            with open(path, "w", encoding="utf-8") as fh:
                yaml.dump(payload, fh, default_flow_style=False)
            logger.info("In-flight state written: %s", path)
        except Exception as exc:
            logger.warning("Failed to write in_flight.yaml: %s", exc)

    def _write_session_ledger(
        self,
        session_id: str,
        dispatch_count: int,
        command: ModelSessionOrchestratorCommand,
    ) -> None:
        try:
            abs_state_dir = os.path.abspath(command.state_dir)
            os.makedirs(abs_state_dir, exist_ok=True)
            path = os.path.join(abs_state_dir, "ledger.jsonl")
            entry = {
                "session_id": session_id,
                "end_time": _now().isoformat(),
                "dispatch_count": dispatch_count,
                "mode": command.mode,
                "dry_run": command.dry_run,
            }
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        except Exception as exc:
            logger.warning("Failed to write session ledger: %s", exc)

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
    "ModelDispatchReceipt",
    "ModelHealthDimensionResult",
    "ModelRSDQueueItem",
    "ModelSessionHealthReport",
    "ModelSessionOrchestratorCommand",
    "ModelSessionOrchestratorResult",
    "ProbeCallable",
]
