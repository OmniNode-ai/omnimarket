# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerSessionBootstrap — Session bootstrapper (Rev 7).

Rev 7 changes vs v1:
- CronList pre-check before CronCreate — skips duplicate crons (C5 fix)
- CronOutputVerificationRoutine prompt embedded in pulse cron (C1/C2 fix)
- File-based dispatch lease at .onex_state/dispatch-lock.json (C4 fix)
- EnumDodCheckType replaces free-text check_command (C6 security fix)
- Severity-ranked status accumulator (failed > degraded > ready)
- Writes cron job IDs to .onex_state/session-crons-{session_id}.json
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_session_bootstrap.models.model_session_contract import (
    ModelSessionContract,
)

logger = logging.getLogger(__name__)

_COST_CEILING_WARNING_THRESHOLD: float = 20.0
_DISPATCH_LEASE_EXPIRY_MINUTES: int = 30

# Phase-1 crons only. Phase-2 entries listed for contract completeness but filtered out.
_REQUIRED_CRONS: list[dict[str, object]] = [
    {
        "cron_name": "build-dispatch-pulse",
        "cron_expr": "*/30 * * * *",
        "active_modes": ["build"],
        "timeout_budget_ms": 300000,
        "phase": 1,
    },
    {
        "cron_name": "merge-sweep",
        "cron_expr": "*/30 * * * *",
        "active_modes": ["build", "close-out"],
        "timeout_budget_ms": 600000,
        "phase": 2,
    },
    {
        "cron_name": "overseer-verify",
        "cron_expr": "0 * * * *",
        "active_modes": ["build", "close-out", "reporting"],
        "timeout_budget_ms": 120000,
        "phase": 2,
    },
]


# ---- CronScheduler protocol (injectable for tests) ----

class CronEntry(BaseModel):
    job_id: str
    name: str


class CronSchedulerProtocol(Protocol):
    def list_crons(self) -> list[CronEntry]: ...
    def create_cron(
        self, *, name: str, cron_expr: str, prompt: str, recurring: bool
    ) -> str: ...


class NullCronScheduler:
    """No-op scheduler used in dry_run mode or when no scheduler is injected."""

    def list_crons(self) -> list[CronEntry]:
        return []

    def create_cron(
        self, *, name: str, cron_expr: str, prompt: str, recurring: bool
    ) -> str:
        return f"(dry-run-job-{name})"


# ---- Input/Output models ----

class ModelBootstrapCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    contract: ModelSessionContract
    state_dir: str = ".onex_state"
    dry_run: bool = False


class EnumBootstrapStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


def _rank(status: EnumBootstrapStatus) -> int:
    return {"ready": 0, "degraded": 1, "failed": 2}[status.value]


class ModelBootstrapResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str
    status: EnumBootstrapStatus
    contract_path: str
    crons_registered: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    dry_run: bool = False
    bootstrapped_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


# ---- Dispatch lease helpers (C4 fix) ----

def acquire_dispatch_lease(state_dir: str, holder: str, tick_id: str) -> bool:
    """Try to acquire the dispatch lease. Returns True if acquired.

    A lease older than 30 minutes is stale and is overwritten with a warning.
    Both build_dispatch_pulse and HandlerBuildLoopExecutor must call this
    before dispatching work.
    """
    lease_path = os.path.join(state_dir, "dispatch-lock.json")
    if os.path.exists(lease_path):
        try:
            with open(lease_path, encoding="utf-8") as fh:
                existing = json.load(fh)
            acquired_at = datetime.fromisoformat(existing.get("acquired_at", ""))
            age = datetime.now(UTC) - acquired_at.replace(tzinfo=UTC)
            if age < timedelta(minutes=_DISPATCH_LEASE_EXPIRY_MINUTES):
                logger.info(
                    "dispatch lease held by %s (tick %s)",
                    existing.get("holder"), existing.get("tick_id"),
                )
                return False
            logger.warning(
                "dispatch lease stale (%s min), overwriting", int(age.total_seconds() / 60)
            )
        except Exception as exc:
            logger.warning("dispatch lease read error, overwriting: %s", exc)

    os.makedirs(state_dir, exist_ok=True)
    lease = {
        "tick_id": tick_id,
        "acquired_at": datetime.now(UTC).isoformat(),
        "holder": holder,
    }
    with open(lease_path, "w", encoding="utf-8") as fh:
        json.dump(lease, fh)
    return True


def release_dispatch_lease(state_dir: str) -> None:
    """Release the dispatch lease. Logs a warning on failure; does not raise."""
    lease_path = os.path.join(state_dir, "dispatch-lock.json")
    try:
        if os.path.exists(lease_path):
            os.remove(lease_path)
    except Exception as exc:
        logger.warning("dispatch lease release failed (expires in 30 min): %s", exc)


# ---- Pulse prompt builder ----

def build_pulse_prompt(cron_name: str, timeout_budget_ms: int, session_id: str) -> str:
    """Build the prompt for a dispatch-pulse cron tick.

    Embeds CronOutputVerificationRoutine instructions so vacuous-pulse detection
    and cross-tick verification happen every tick as prompt-governed behavior.
    """
    timeout_sec = timeout_budget_ms // 1000
    stall_sec = int(timeout_sec * 0.33)
    dead_sec = int(timeout_sec * 0.66)

    return f"""# {cron_name} — session {session_id}

## FIRST ACTION: Cross-tick verification (skip on first-ever tick)

1. Find the latest .onex_state/pulse-ticks/*.json
2. For each task_id in prev_tick.dispatched_task_ids:
   - Verify .onex_state/dispatch-events/{{prev_tick_id}}-{{task_id}}.json exists
   - Missing file → log HALLUCINATED_PASS, emit onex.evt.omnimarket.session-cron-health-violation.v1
3. If tick file is missing (and not first tick): prev tick failed silently → ESCALATE

## Stall detection (run BEFORE dispatching new work)

Thresholds (derived from timeout_budget_ms={timeout_budget_ms}):
  stall = {stall_sec}s, dead = {dead_sec}s

For each in_progress task:
  - > {stall_sec}s since last update → STALLED: send one status ping
  - > {dead_sec}s since last update → DEAD: narrow scope, respawn (cap 3 attempts)
  - After 3rd failure → escalate to user
  - Log each respawn: .onex_state/friction/respawn-{{task_id}}-attempt-{{n}}.json

## False completion detection

When task marked done: check PR exists + branch has commits.
If neither → reopen task, log .onex_state/friction/false-completion-{{task_id}}.json

## Dispatch loop

1. Acquire lease: .onex_state/dispatch-lock.json (holder={cron_name})
   If held → skip dispatch this tick, log INFO
2. Pull Linear active sprint. Classify unworked tickets.
3. For each unworked ticket:
   a. tick_id = "tick-YYYYMMDD-HHMM"
   b. Write .onex_state/task-contracts/{{task_id}}.json (ModelTaskContract)
   c. Write .onex_state/dispatch-events/{{tick_id}}-{{task_id}}.json
   d. Dispatch: mechanical → node_dispatch_worker (dogfood) or Agent fallback
                reasoning → Sonnet
4. Release lease in finally block.

## Post-tick: CronOutputVerificationRoutine

dispatched_count = number of dispatch-event files written this tick

Gate 1: if backlog_unworked_count > 0 and dispatched_count == 0:
  emit VACUOUS_PULSE → onex.evt.omnimarket.session-cron-health-violation.v1
  write .onex_state/friction/vacuous-pulse-{{timestamp}}.json
  verdict = "fail"

Gate 2: if dispatch_path == "agent_bypass" and dogfood_available:
  log WARNING "Dispatch used Agent bypass. node_dispatch_worker was available."

Gate 3: if backlog_unworked_count == 0: verdict = "pass"

Write .onex_state/pulse-ticks/{{tick_id}}.json:
  {{ tick_id, dispatched_count, dispatched_task_ids, backlog_unworked_count,
     dispatch_path_used, verdict }}
"""


# ---- Main handler ----

class HandlerSessionBootstrap:
    """Session bootstrap orchestrator (Rev 7).

    Validates contract, calls CronCreate for phase-1 crons (idempotent via
    CronList pre-check), writes cron job IDs to disk, returns ModelBootstrapResult.

    Inject `cron_scheduler` to override cron I/O (used in tests and dry_run).
    """

    def __init__(self, cron_scheduler: CronSchedulerProtocol | None = None) -> None:
        self._cron_scheduler = cron_scheduler

    def handle(self, command: ModelBootstrapCommand) -> ModelBootstrapResult:
        warnings: list[str] = []
        status = EnumBootstrapStatus.READY
        crons_registered: list[str] = []

        def _escalate(new_status: EnumBootstrapStatus, msg: str) -> None:
            nonlocal status
            if _rank(new_status) > _rank(status):
                status = new_status
            warnings.append(msg)
            logger.warning("Bootstrap: %s", msg)

        if not command.contract.phases_expected:
            _escalate(
                EnumBootstrapStatus.DEGRADED,
                "phases_expected is empty — no phases will be tracked for this session",
            )

        if command.contract.cost_ceiling_usd > _COST_CEILING_WARNING_THRESHOLD:
            _escalate(
                EnumBootstrapStatus.DEGRADED,
                f"cost_ceiling_usd={command.contract.cost_ceiling_usd} exceeds "
                f"advisory threshold of {_COST_CEILING_WARNING_THRESHOLD}",
            )

        state_dir = os.path.abspath(command.state_dir)

        contract_path = "(dry-run)"
        if not command.dry_run:
            try:
                contract_path = self._write_contract(command, state_dir)
            except Exception as exc:
                _escalate(EnumBootstrapStatus.DEGRADED, f"contract write failed: {exc}")

        if not command.dry_run:
            scheduler: CronSchedulerProtocol = self._cron_scheduler or NullCronScheduler()
            try:
                existing_list = scheduler.list_crons()
                existing = {c.name: c.job_id for c in existing_list}
            except Exception as exc:
                _escalate(EnumBootstrapStatus.DEGRADED, f"CronList failed: {exc}")
                existing = {}

            failed_crons = 0
            for cron_def in _REQUIRED_CRONS:
                if cron_def["phase"] != 1:
                    continue
                if command.contract.session_mode not in cron_def["active_modes"]:
                    continue
                cron_name = str(cron_def["cron_name"])

                if cron_name in existing:
                    job_id = existing[cron_name]
                    logger.info("cron already registered: %s (job_id=%s)", cron_name, job_id)
                    crons_registered.append(job_id)
                    continue

                prompt = build_pulse_prompt(
                    cron_name=cron_name,
                    timeout_budget_ms=int(cron_def["timeout_budget_ms"]),  # type: ignore[arg-type]
                    session_id=command.session_id,
                )
                try:
                    job_id = scheduler.create_cron(
                        name=cron_name,
                        cron_expr=str(cron_def["cron_expr"]),
                        prompt=prompt,
                        recurring=True,
                    )
                    crons_registered.append(job_id)
                    logger.info("cron created: %s (job_id=%s)", cron_name, job_id)
                except Exception as exc:
                    failed_crons += 1
                    _escalate(
                        EnumBootstrapStatus.DEGRADED,
                        f"CronCreate failed for {cron_name}: {exc}",
                    )

            if failed_crons > 0 and not crons_registered:
                _escalate(EnumBootstrapStatus.FAILED, "all cron registrations failed")

            if crons_registered:
                try:
                    self._write_cron_ids(state_dir, command.session_id, crons_registered)
                except Exception as exc:
                    _escalate(
                        EnumBootstrapStatus.DEGRADED,
                        f"cron ID artifact write failed: {exc}",
                    )

        logger.info(
            "Bootstrap complete: session_id=%s status=%s contract_path=%s crons=%s",
            command.session_id, status.value, contract_path, crons_registered,
        )

        return ModelBootstrapResult(
            session_id=command.session_id,
            status=status,
            contract_path=contract_path,
            crons_registered=crons_registered,
            warnings=warnings,
            dry_run=command.dry_run,
        )

    def _write_contract(self, command: ModelBootstrapCommand, state_dir: str) -> str:
        os.makedirs(state_dir, exist_ok=True)
        filename = f"session-contract-{command.session_id}.json"
        path = os.path.join(state_dir, filename)
        payload = command.contract.model_dump()
        payload["session_id"] = command.session_id
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2, default=str))
        return path

    def _write_cron_ids(
        self, state_dir: str, session_id: str, job_ids: list[str]
    ) -> None:
        os.makedirs(state_dir, exist_ok=True)
        path = os.path.join(state_dir, f"session-crons-{session_id}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"session_id": session_id, "cron_job_ids": job_ids}, fh, indent=2)


__all__: list[str] = [
    "CronEntry",
    "CronSchedulerProtocol",
    "EnumBootstrapStatus",
    "HandlerSessionBootstrap",
    "ModelBootstrapCommand",
    "ModelBootstrapResult",
    "NullCronScheduler",
    "acquire_dispatch_lease",
    "build_pulse_prompt",
    "release_dispatch_lease",
]
