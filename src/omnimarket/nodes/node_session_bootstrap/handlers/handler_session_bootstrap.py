# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerSessionBootstrap — Session bootstrapper (Rev 7).

Reads ModelSessionContract, writes a contract snapshot to .onex_state/,
calls CronCreate for required crons (idempotent via CronList pre-check),
acquires/releases a dispatch lease, and returns a structured result.
Runs FIRST each session to initialize the session.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_session_bootstrap.models.model_session_contract import (
    ModelSessionContract,
)

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_COST_CEILING_WARNING_THRESHOLD: float = 20.0

# Required crons activated for "build" session_mode (Phase 1 scope).
# Mirrors required_crons.build_dispatch_pulse in contract.yaml.
_BUILD_DISPATCH_PULSE_CRON_NAME = "build-dispatch-pulse"
_BUILD_DISPATCH_PULSE_INTERVAL_CRON = "*/30 * * * *"
_BUILD_DISPATCH_PULSE_TIMEOUT_BUDGET_MS = 300_000

# Dispatch lease path relative to state_dir
_DISPATCH_LOCK_FILENAME = "dispatch-lock.json"
# Leases older than 30 min are considered stale
_LEASE_STALE_SECONDS = 1800


class ModelBootstrapCommand(BaseModel):
    """Input command for the session bootstrap handler (Rev 7)."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    session_mode: str = "build"  # build | close-out | reporting
    active_sprint_id: str = "auto-detect"
    model_routing_preference: str = "local-first"
    contract: ModelSessionContract
    state_dir: str = ".onex_state"
    dry_run: bool = False


class EnumBootstrapStatus(StrEnum):
    """Terminal status for a bootstrap run."""

    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


class ModelBootstrapResult(BaseModel):
    """Result produced by HandlerSessionBootstrap (Rev 7)."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    status: EnumBootstrapStatus
    contract_path: str
    crons_registered: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    dry_run: bool = False
    bootstrapped_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


# ---------------------------------------------------------------------------
# Protocols for injected SDK tools (allow testability without real SDK)
# ---------------------------------------------------------------------------


class CronListResult(Protocol):
    """Minimal protocol for a CronList result entry."""

    @property
    def name(self) -> str: ...


class CronCreateResult(Protocol):
    """Minimal protocol for CronCreate result."""

    @property
    def job_id(self) -> str: ...


class CronListTool(Protocol):
    def __call__(self) -> list[CronListResult]: ...


class CronCreateTool(Protocol):
    def __call__(
        self, *, cron: str, prompt: str, recurring: bool
    ) -> CronCreateResult: ...


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class HandlerSessionBootstrap:
    """Session bootstrap orchestrator (Rev 7).

    Idempotent CronCreate via CronList pre-check (C5).
    File-based dispatch lease (C4).
    crons_registered replaces timer_configs in result.
    """

    def __init__(
        self,
        cron_list: CronListTool | None = None,
        cron_create: CronCreateTool | None = None,
    ) -> None:
        self._cron_list = cron_list
        self._cron_create = cron_create

    def handle(self, command: ModelBootstrapCommand) -> ModelBootstrapResult:
        warnings: list[str] = []
        crons_registered: list[str] = []

        # Validate phases_expected
        if not command.contract.phases_expected:
            warnings.append(
                "phases_expected is empty — no phases will be tracked for this session"
            )
            logger.warning("Bootstrap: phases_expected is empty")

        # Advisory cost ceiling check
        if command.contract.cost_ceiling_usd > _COST_CEILING_WARNING_THRESHOLD:
            warnings.append(
                f"cost_ceiling_usd={command.contract.cost_ceiling_usd} exceeds "
                f"advisory threshold of {_COST_CEILING_WARNING_THRESHOLD}"
            )

        state_dir = os.path.abspath(command.state_dir)

        # Write contract snapshot
        contract_path = "(dry-run)"
        if not command.dry_run:
            try:
                contract_path = self._write_contract(command, state_dir)
            except OSError as exc:
                warnings.append(f"contract snapshot write failed: {exc}")
                logger.warning("Bootstrap: contract write failed: %s", exc)

        # Register required crons (build mode only in Phase 1)
        if command.session_mode == "build":
            job_id = self._register_build_dispatch_pulse(
                command, state_dir, warnings
            )
            if job_id:
                crons_registered.append(job_id)

        # Write cron IDs to state file
        if not command.dry_run and crons_registered:
            self._write_cron_ids(command.session_id, crons_registered, state_dir)

        # Status mapping per spec
        if not crons_registered and command.session_mode == "build" and not command.dry_run:
            status = (
                EnumBootstrapStatus.FAILED
                if len(warnings) > 1
                else EnumBootstrapStatus.DEGRADED
            )
        elif warnings:
            status = EnumBootstrapStatus.DEGRADED
        else:
            status = EnumBootstrapStatus.READY

        logger.info(
            "Bootstrap complete: session_id=%s status=%s crons=%s",
            command.session_id,
            status.value,
            crons_registered,
        )

        return ModelBootstrapResult(
            session_id=command.session_id,
            status=status,
            contract_path=contract_path,
            crons_registered=crons_registered,
            warnings=warnings,
            dry_run=command.dry_run,
        )

    # ------------------------------------------------------------------
    # Cron registration (C5: idempotent via CronList pre-check)
    # ------------------------------------------------------------------

    def _register_build_dispatch_pulse(
        self,
        command: ModelBootstrapCommand,
        state_dir: str,
        warnings: list[str],
    ) -> str | None:
        if command.dry_run:
            return "dry-run-job-id"

        if self._cron_list is None or self._cron_create is None:
            warnings.append(
                "CronList/CronCreate tools not injected — skipping cron registration"
            )
            return None

        # C5: check existing crons before creating
        try:
            existing = self._cron_list()
            existing_names = {entry.name for entry in existing}
        except Exception as exc:
            warnings.append(f"CronList failed: {exc} — skipping cron registration")
            logger.warning("Bootstrap: CronList failed: %s", exc)
            return None

        if _BUILD_DISPATCH_PULSE_CRON_NAME in existing_names:
            logger.info(
                "Bootstrap: cron already registered: %s",
                _BUILD_DISPATCH_PULSE_CRON_NAME,
            )
            warnings.append(
                f"cron already registered (skipped CronCreate): {_BUILD_DISPATCH_PULSE_CRON_NAME}"
            )
            # Return a sentinel so caller knows the cron is active
            return f"existing:{_BUILD_DISPATCH_PULSE_CRON_NAME}"

        prompt = self._build_pulse_prompt(
            _BUILD_DISPATCH_PULSE_CRON_NAME,
            dispatch_count_min=1,
            timeout_budget_ms=_BUILD_DISPATCH_PULSE_TIMEOUT_BUDGET_MS,
        )
        try:
            result = self._cron_create(
                cron=_BUILD_DISPATCH_PULSE_INTERVAL_CRON,
                prompt=prompt,
                recurring=True,
            )
            return result.job_id
        except Exception as exc:
            warnings.append(f"CronCreate failed for {_BUILD_DISPATCH_PULSE_CRON_NAME}: {exc}")
            logger.warning("Bootstrap: CronCreate failed: %s", exc)
            return None

    def _build_pulse_prompt(
        self,
        cron_name: str,
        dispatch_count_min: int,
        timeout_budget_ms: int,
    ) -> str:
        timeout_budget_sec = timeout_budget_ms // 1000
        stall_threshold_sec = int(timeout_budget_sec * 0.33)
        dead_threshold_sec = int(timeout_budget_sec * 0.66)
        return (
            f"# {cron_name} — build dispatch pulse\n\n"
            "FIRST ACTION: Read previous tick result from .onex_state/pulse-ticks/. "
            "If prev tick verdict=='pass' and dispatched_task_ids is non-empty, "
            "verify each task_id has a matching .onex_state/dispatch-events/<prev_tick_id>-<task_id>.json. "
            "If any file is missing: emit HALLUCINATED_PASS health-violation. "
            "If prev tick file is missing (and not first tick): ESCALATE to user.\n\n"
            "THEN:\n"
            "1. Acquire dispatch lease: read .onex_state/dispatch-lock.json. "
            f"If acquired_at < {_LEASE_STALE_SECONDS}s ago: log INFO 'lease held', return SKIPPED. "
            "If stale: log WARNING 'stale lease, overwriting'. Write lease file.\n"
            "2. Check in-progress tasks for stalls: "
            f"STALLED if >{stall_threshold_sec}s since last update; "
            f"DEAD if >{dead_threshold_sec}s. Send status ping to stalled; respawn dead (max 3 attempts).\n"
            "3. Pull Linear active sprint. Classify unworked tickets (mechanical vs reasoning).\n"
            "4. For each unworked ticket: acquire dispatch lease, dispatch worker "
            "(dogfood path if node_dispatch_worker deployed, else Agent fallback). "
            "Write .onex_state/dispatch-events/<tick_id>-<task_id>.json for each dispatch.\n"
            "5. CronOutputVerificationRoutine:\n"
            "   - dispatched = count of dispatch-event files written this tick\n"
            f"   - if backlog_unworked_count > 0 AND dispatched == 0: emit VACUOUS_PULSE to "
            "onex.evt.omnimarket.session-cron-health-violation.v1; write friction; verdict=fail\n"
            "   - if dispatch_path_used == 'agent_bypass' AND dogfood_available: log WARNING bypass\n"
            "   - write .onex_state/pulse-ticks/<tick_id>.json with dispatched_task_ids and verdict\n"
            "6. Release dispatch lease (delete .onex_state/dispatch-lock.json).\n"
            "7. Report: dispatched_count, backlog_unworked_count, dispatch_path_used, verdict.\n"
            f"\nRequired: dispatch_count_per_tick_min={dispatch_count_min} when backlog_unworked_count > 0."
        )

    # ------------------------------------------------------------------
    # Dispatch lease (C4)
    # ------------------------------------------------------------------

    def acquire_dispatch_lease(
        self, state_dir: str, tick_id: str, holder: str
    ) -> bool:
        """Acquire the dispatch lease. Returns True if acquired, False if held."""
        lock_path = os.path.join(os.path.abspath(state_dir), _DISPATCH_LOCK_FILENAME)
        now = datetime.now(tz=UTC)

        if os.path.exists(lock_path):
            try:
                with open(lock_path) as f:
                    existing = json.load(f)
                acquired_at = datetime.fromisoformat(existing.get("acquired_at", ""))
                age_seconds = (now - acquired_at).total_seconds()
                if age_seconds < _LEASE_STALE_SECONDS:
                    logger.info(
                        "Dispatch lease held by %s (acquired %ds ago)",
                        existing.get("holder"),
                        int(age_seconds),
                    )
                    return False
                logger.warning(
                    "Stale dispatch lease (age=%ds), overwriting", int(age_seconds)
                )
            except (OSError, ValueError, KeyError):
                pass

        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        with open(lock_path, "w") as f:
            json.dump(
                {
                    "tick_id": tick_id,
                    "acquired_at": now.isoformat(),
                    "holder": holder,
                },
                f,
                indent=2,
            )
        return True

    def release_dispatch_lease(self, state_dir: str) -> None:
        """Release the dispatch lease by deleting the lock file."""
        lock_path = os.path.join(os.path.abspath(state_dir), _DISPATCH_LOCK_FILENAME)
        try:
            os.remove(lock_path)
        except OSError as exc:
            logger.warning("Failed to release dispatch lease: %s", exc)

    # ------------------------------------------------------------------
    # Filesystem helpers
    # ------------------------------------------------------------------

    def _write_contract(
        self, command: ModelBootstrapCommand, state_dir: str
    ) -> str:
        os.makedirs(state_dir, exist_ok=True)
        filename = f"session-contract-{command.session_id}.json"
        path = os.path.join(state_dir, filename)
        payload = command.contract.model_dump()
        payload["session_id"] = command.session_id
        payload["session_mode"] = command.session_mode
        payload["active_sprint_id"] = command.active_sprint_id
        payload["model_routing_preference"] = command.model_routing_preference
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2, default=str))
        return path

    def _write_cron_ids(
        self, session_id: str, cron_ids: list[str], state_dir: str
    ) -> None:
        os.makedirs(state_dir, exist_ok=True)
        path = os.path.join(state_dir, f"session-crons-{session_id}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"session_id": session_id, "cron_job_ids": cron_ids}, fh, indent=2)


__all__: list[str] = [
    "EnumBootstrapStatus",
    "HandlerSessionBootstrap",
    "ModelBootstrapCommand",
    "ModelBootstrapResult",
]
