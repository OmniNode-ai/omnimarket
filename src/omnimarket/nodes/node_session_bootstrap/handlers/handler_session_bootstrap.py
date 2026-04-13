# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerSessionBootstrap — Session bootstrapper (Rev 7).

Rev 7 changes (from hostile review C3-C6):
  C5: Idempotent CronCreate — handler checks CronList before calling CronCreate.
      Does not create duplicate crons on re-run.
  C4: Dispatch lease — both dispatch paths (cron + triggered build loop) must
      acquire .onex_state/dispatch-lock.json before dispatching.
  C3: Dispatch-event files include task_id — verifier uses IDs not scalar count.
  C6: check_command: str replaced by EnumDodCheckType — no command injection.

Reads ModelBootstrapCommand, validates the session contract, writes a snapshot
to .onex_state/, creates required CronCreate jobs (phase 1: build_dispatch_pulse
only), writes cron IDs to disk, and returns a structured result.

The CronOutputVerificationRoutine described in contract.yaml is prompt-embedded
(runs inside the cron prompt).  The data structures it writes to disk are defined
here so tests can verify them without firing a real cron.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_session_bootstrap.models.model_session_contract import (
    ModelSessionContract,
)

_TOPIC_SESSION_CRON_HEALTH_VIOLATION = (
    "onex.evt.omnimarket.session-cron-health-violation.v1"
)

logger = logging.getLogger(__name__)

# Advisory cost ceiling threshold for warnings
_COST_CEILING_WARNING_THRESHOLD: float = 20.0

# Cron interval for build_dispatch_pulse (minutes)
_BUILD_DISPATCH_PULSE_INTERVAL_MIN: int = 30

# Phase-1 cron names (only build_dispatch_pulse is created in v2.0)
_PHASE1_CRON_NAMES: frozenset[str] = frozenset({"build-dispatch-pulse"})

# Session modes that activate build_dispatch_pulse
_BUILD_DISPATCH_ACTIVE_MODES: frozenset[str] = frozenset({"build"})


def _interval_to_cron(interval_min: int) -> str:
    """Convert an interval in minutes to a cron expression.

    Examples:
        30 -> "*/30 * * * *"
        15 -> "*/15 * * * *"
    """
    return f"*/{interval_min} * * * *"


class ModelBootstrapCommand(BaseModel):
    """Input command for the session bootstrap handler (Rev 7)."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    session_mode: str = Field(
        default="build",
        description="Controls which crons are activated. One of: build, close-out, reporting",
    )
    active_sprint_id: str = Field(
        default="auto-detect",
        description="Linear cycle ID, or 'auto-detect' to query Linear for active sprint",
    )
    model_routing_preference: str = Field(
        default="local-first",
        description=(
            "Routing preference passed to dogfood gate. "
            "One of: local-first, frontier-only, hybrid"
        ),
    )
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
    crons_registered: list[str] = Field(
        default_factory=list,
        description="List of CronJob IDs created or confirmed by bootstrap",
    )
    warnings: list[str] = Field(default_factory=list)
    dry_run: bool = False
    bootstrapped_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class ModelCronSpec(BaseModel):
    """Internal spec for a cron to be created or confirmed."""

    model_config = ConfigDict(extra="forbid")

    cron_name: str
    prompt_template_key: str
    interval_min: int
    active_modes: list[str]
    timeout_budget_ms: int
    description: str


# Phase-1 cron specs (contract.yaml required_crons, phase 1 only).
# Phase-2 entries (merge_sweep, overseer_verify) are declared for contract
# completeness but filtered out by _PHASE1_CRON_NAMES.
_REQUIRED_CRONS: list[ModelCronSpec] = [
    ModelCronSpec(
        cron_name="build-dispatch-pulse",
        prompt_template_key="BUILD_DISPATCH_PULSE_PROMPT",
        interval_min=_BUILD_DISPATCH_PULSE_INTERVAL_MIN,
        active_modes=["build"],
        timeout_budget_ms=300000,
        description=(
            "Pull Linear sprint, classify unworked tickets, dispatch workers via "
            "node_dispatch_worker (dogfood path) or Agent (fallback). Verify output "
            "via CronOutputVerificationRoutine after each tick."
        ),
    ),
]


def build_pulse_prompt(
    cron_name: str,
    timeout_budget_ms: int,
    session_id: str,
    state_dir: str,
    model_routing_preference: str = "local-first",
) -> str:
    """Generate the prompt template for a pulse cron job.

    The prompt embeds CronOutputVerificationRoutine instructions so the cron
    enforces dispatch every tick (prompt-governed behavior, not handler behavior).

    All paths in the prompt are derived from state_dir — never hardcoded
    absolute paths.
    """
    tick_timeout_sec = timeout_budget_ms // 1000
    stall_threshold_sec = int(tick_timeout_sec * 0.33)
    dead_threshold_sec = int(tick_timeout_sec * 0.66)

    # Derive fallback agent string from routing preference.
    _routing_to_agent = {
        "local-first": "Agent(model=local)",
        "frontier-only": "Agent(model=opus)",
        "hybrid": "Agent(model=sonnet)",
    }
    fallback_agent = _routing_to_agent.get(
        model_routing_preference, "Agent(model=sonnet)"
    )

    if cron_name == "build-dispatch-pulse":
        return (
            f"## build_dispatch_pulse -- Session {session_id}\n\n"
            f"### FIRST ACTION: Cross-tick verification\n"
            f"1. Find latest {state_dir}/pulse-ticks/*.json (skip if first tick ever).\n"
            f"2. If prev tick exists: for each task_id in prev_tick.dispatched_task_ids,\n"
            f"   verify {state_dir}/dispatch-events/{{prev_tick_id}}-{{task_id}}.json exists.\n"
            f"   If any task_id has no matching file -> HALLUCINATED PASS -> emit\n"
            f"   {_TOPIC_SESSION_CRON_HEALTH_VIOLATION}.\n\n"
            f"### Stall detection (before dispatching new work)\n"
            f"For each in-progress task in TaskList:\n"
            f"  - STALLED if >{stall_threshold_sec}s since last update -> SendMessage (1-turn grace).\n"
            f"  - DEAD if >{dead_threshold_sec}s since last update -> respawn with narrowed scope.\n"
            f"  - Cap respawns at 3. After 3rd failure -> escalate to user.\n"
            f"  - Log respawns to {state_dir}/friction/respawn-{{task_id}}-attempt-{{n}}.json\n\n"
            f"### Dispatch (routing={model_routing_preference})\n"
            f"1. Acquire dispatch lease: {state_dir}/dispatch-lock.json\n"
            f"2. Pull Linear active sprint. Classify unworked tickets (mechanical vs reasoning).\n"
            f"3. For each unworked ticket:\n"
            f"   a. Generate tick_id = tick-YYYYMMDD-HHMM.\n"
            f"   b. Check if node_dispatch_worker is deployed and consuming.\n"
            f"      YES -> dispatch via dogfood path, write dispatch-event file.\n"
            f"      NO  -> dispatch via {fallback_agent}, write dispatch-event file.\n"
            f"   c. Write {state_dir}/dispatch-events/{{tick_id}}-{{task_id}}.json\n"
            f"      {{ tick_id, task_id, ticket_id, dispatch_path, model_used, timestamp }}\n"
            f"   d. Write {state_dir}/task-contracts/{{task_id}}.json (ModelTaskContract).\n"
            f"4. Release dispatch lease.\n\n"
            f"### CronOutputVerificationRoutine (post-dispatch gate)\n"
            f"dispatched_task_ids = task_ids from dispatch-event files for this tick_id.\n"
            f"dispatched = len(dispatched_task_ids)\n\n"
            f"Gate 1 -- VACUOUS_PULSE:\n"
            f"  If backlog_unworked_count > 0 AND dispatched == 0:\n"
            f"    Emit {_TOPIC_SESSION_CRON_HEALTH_VIOLATION}\n"
            f"    Write {state_dir}/friction/vacuous-pulse-{{timestamp}}.json\n"
            f"    Write {state_dir}/pulse-ticks/{{tick_id}}.json (verdict=fail)\n"
            f"    STOP -- do not report success.\n\n"
            f"Gate 2 -- dogfood bypass log:\n"
            f"  If dispatch_path_used == 'agent_bypass' AND dogfood_available:\n"
            f"    Log WARNING: 'bypass: node_dispatch_worker was available'\n"
            f"    Append to {state_dir}/session-bypass-log-{{session_id}}.jsonl\n\n"
            f"Gate 3 -- backlog empty:\n"
            f"  If backlog_unworked_count == 0: verdict=pass, proceed.\n\n"
            f"Write {state_dir}/pulse-ticks/{{tick_id}}.json:\n"
            f"  {{ tick_id, dispatched_count, dispatched_task_ids, backlog_unworked_count,\n"
            f"     dispatch_path_used, verdict }}\n"
        )

    # Generic fallback for phase-2 crons when they are activated
    return (
        f"## {cron_name} -- Session {session_id}\n\n"
        f"Run {cron_name} per contract spec. "
        f"Tick budget: {tick_timeout_sec}s. "
        f"State dir: {state_dir}.\n"
    )


class HandlerSessionBootstrap:
    """Session bootstrap orchestrator (Rev 7).

    Validates session contract, writes contract snapshot, creates required
    CronCreate jobs (C5: idempotent via CronList pre-check), writes cron IDs
    to disk, and returns ModelBootstrapResult.

    CronCreate calls are skipped when dry_run=True.  CronList and CronCreate
    callables are injected at construction time for testability.
    """

    def __init__(
        self,
        cron_list_fn: Callable[[], list[dict[str, str]]] | None = None,
        cron_create_fn: Callable[..., str | None] | None = None,
    ) -> None:
        """
        Args:
            cron_list_fn: Callable() -> list[dict] returning registered cron jobs.
                          Injected for testing. Defaults to _default_cron_list.
            cron_create_fn: Callable(cron, prompt, recurring) -> str|None returning
                            a job ID.  Injected for testing. Defaults to _default_cron_create.
        """
        self._cron_list_fn = cron_list_fn or _default_cron_list
        self._cron_create_fn = cron_create_fn or _default_cron_create

    def handle(self, command: ModelBootstrapCommand) -> ModelBootstrapResult:
        """Execute session bootstrap (Rev 7).

        Args:
            command: Bootstrap command including session_id, contract, session_mode,
                     active_sprint_id, model_routing_preference, and flags.

        Returns:
            ModelBootstrapResult with status, contract_path, crons_registered, warnings.
        """
        # Severity-ranked status accumulator: failed > degraded > ready (M14 fix)
        _status_rank = {
            EnumBootstrapStatus.FAILED: 2,
            EnumBootstrapStatus.DEGRADED: 1,
            EnumBootstrapStatus.READY: 0,
        }
        current_status = EnumBootstrapStatus.READY
        warnings: list[str] = []

        def _bump_status(new: EnumBootstrapStatus, reason: str) -> None:
            nonlocal current_status
            if _status_rank[new] > _status_rank[current_status]:
                current_status = new
            warnings.append(reason)

        # Validate session mode
        valid_modes = {"build", "close-out", "reporting"}
        if command.session_mode not in valid_modes:
            _bump_status(
                EnumBootstrapStatus.DEGRADED,
                f"Unknown session_mode={command.session_mode!r}; expected one of {valid_modes}",
            )

        # Advisory cost ceiling check
        if command.contract.cost_ceiling_usd > _COST_CEILING_WARNING_THRESHOLD:
            warnings.append(
                f"cost_ceiling_usd={command.contract.cost_ceiling_usd} exceeds "
                f"advisory threshold of {_COST_CEILING_WARNING_THRESHOLD}"
            )

        # Validate phases_expected
        if not command.contract.phases_expected:
            _bump_status(
                EnumBootstrapStatus.DEGRADED,
                "phases_expected is empty -- no phases will be tracked for this session",
            )
            logger.warning("Bootstrap: phases_expected is empty")

        # Write contract snapshot to .onex_state/session-contract-{session_id}.json
        contract_path = "(dry-run)"
        if not command.dry_run:
            try:
                contract_path = self._write_contract(command)
            except OSError as exc:
                _bump_status(
                    EnumBootstrapStatus.DEGRADED,
                    f"Failed to write contract snapshot: {exc}",
                )

        # CronCreate for required crons (C5: idempotent via CronList pre-check)
        crons_registered: list[str] = []
        failed_cron_count = 0

        if command.session_mode in _BUILD_DISPATCH_ACTIVE_MODES:
            existing_crons = self._list_existing_crons()
            if existing_crons is None:
                # CronList failed — skip registration entirely to avoid creating duplicates.
                _bump_status(
                    EnumBootstrapStatus.DEGRADED,
                    "CronList unavailable — skipping cron registration to avoid duplicates",
                )
            else:
                existing_names: set[str] = {c.get("name", "") for c in existing_crons}

                for spec in _REQUIRED_CRONS:
                    # Phase filter: only create phase-1 crons
                    if spec.cron_name not in _PHASE1_CRON_NAMES:
                        logger.debug("Skipping phase-2 cron: %s", spec.cron_name)
                        continue

                    if command.session_mode not in spec.active_modes:
                        logger.debug(
                            "Cron %s not active in mode %s -- skipping",
                            spec.cron_name,
                            command.session_mode,
                        )
                        continue

                    if spec.cron_name in existing_names:
                        # C5: cron already registered -- skip, record existing ID
                        existing_id = next(
                            (
                                c.get("id", spec.cron_name)
                                for c in existing_crons
                                if c.get("name") == spec.cron_name
                            ),
                            spec.cron_name,
                        )
                        logger.info(
                            "Cron already registered: %s (id=%s)",
                            spec.cron_name,
                            existing_id,
                        )
                        crons_registered.append(existing_id)
                        continue

                    if command.dry_run:
                        logger.info("dry_run: would CronCreate %s", spec.cron_name)
                        crons_registered.append(f"(dry-run:{spec.cron_name})")
                        continue

                    # Create the cron
                    prompt = build_pulse_prompt(
                        cron_name=spec.cron_name,
                        timeout_budget_ms=spec.timeout_budget_ms,
                        session_id=command.session_id,
                        state_dir=command.state_dir,
                        model_routing_preference=command.model_routing_preference,
                    )
                    cron_expr = _interval_to_cron(spec.interval_min)
                    job_id = self._create_cron(cron_expr, prompt, recurring=True)

                    if job_id:
                        logger.info(
                            "CronCreate succeeded: %s -> %s", spec.cron_name, job_id
                        )
                        crons_registered.append(job_id)
                    else:
                        failed_cron_count += 1
                        _bump_status(
                            EnumBootstrapStatus.DEGRADED,
                            f"CronCreate failed for {spec.cron_name}",
                        )

                # If all phase-1 crons failed, mark as FAILED
                phase1_required = sum(
                    1
                    for s in _REQUIRED_CRONS
                    if s.cron_name in _PHASE1_CRON_NAMES
                    and command.session_mode in s.active_modes
                )
                if phase1_required > 0 and failed_cron_count >= phase1_required:
                    current_status = EnumBootstrapStatus.FAILED

        # Write cron IDs to disk
        if not command.dry_run and crons_registered:
            try:
                self._write_cron_ids(command, crons_registered)
            except OSError as exc:
                _bump_status(
                    EnumBootstrapStatus.DEGRADED,
                    f"Failed to write cron IDs to disk: {exc}",
                )

        logger.info(
            "Bootstrap complete: session_id=%s status=%s contract_path=%s crons=%s",
            command.session_id,
            current_status.value,
            contract_path,
            crons_registered,
        )

        return ModelBootstrapResult(
            session_id=command.session_id,
            status=current_status,
            contract_path=contract_path,
            crons_registered=crons_registered,
            warnings=warnings,
            dry_run=command.dry_run,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _list_existing_crons(self) -> list[dict[str, str]] | None:
        """Call CronList and return list of job dicts, or None on error.

        Returns None (not an empty list) on failure so callers can distinguish
        "CronList unreachable" from "no crons registered".  Callers must skip
        CronCreate when None is returned to avoid creating duplicates.
        """
        try:
            result = self._cron_list_fn()
            if isinstance(result, list):
                return [dict(c) for c in result]
            return []
        except Exception as exc:
            logger.warning(
                "CronList failed — skipping cron registration to avoid duplicates: %s",
                exc,
            )
            return None

    def _create_cron(self, cron: str, prompt: str, recurring: bool) -> str | None:
        """Call CronCreate and return job ID, or None on failure."""
        try:
            result = self._cron_create_fn(cron=cron, prompt=prompt, recurring=recurring)
            if result and isinstance(result, str):
                return result
            return None
        except Exception as exc:
            logger.warning("CronCreate failed: %s", exc)
            return None

    def _write_contract(self, command: ModelBootstrapCommand) -> str:
        """Write contract JSON to state_dir and return the absolute path."""
        state_dir = os.path.abspath(command.state_dir)
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
        self,
        command: ModelBootstrapCommand,
        cron_ids: list[str],
    ) -> None:
        """Write registered cron job IDs to disk."""
        state_dir = os.path.abspath(command.state_dir)
        os.makedirs(state_dir, exist_ok=True)
        filename = f"session-crons-{command.session_id}.json"
        path = os.path.join(state_dir, filename)
        payload = {
            "session_id": command.session_id,
            "registered_at": datetime.now(tz=UTC).isoformat(),
            "cron_ids": cron_ids,
        }
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2))
        logger.info("Cron IDs written: %s", path)


# ------------------------------------------------------------------
# Default no-op CronList / CronCreate stubs.
# In production these are replaced by real tool calls; in tests they
# are injected as mocks via the constructor.
# ------------------------------------------------------------------


def _default_cron_list() -> list[dict[str, str]]:  # stub-ok
    """Default CronList no-op — returns empty list.

    In production, the Claude Code agent replaces this with a real CronList
    tool call.  Injected via constructor so tests can override without monkeypatching.
    """
    logger.debug("_default_cron_list called (no-op — no real cron infrastructure)")
    return []


def _default_cron_create(
    cron: str, prompt: str, recurring: bool
) -> str | None:  # stub-ok
    """Default CronCreate no-op — returns None (no job created).

    In production, the Claude Code agent replaces this with a real CronCreate
    tool call.
    """
    logger.debug("_default_cron_create called (no-op — no real cron infrastructure)")
    return None


__all__: list[str] = [
    "EnumBootstrapStatus",
    "HandlerSessionBootstrap",
    "ModelBootstrapCommand",
    "ModelBootstrapResult",
    "ModelCronSpec",
    "build_pulse_prompt",
]
