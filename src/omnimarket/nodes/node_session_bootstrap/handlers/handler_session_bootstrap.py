# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerSessionBootstrap — Session bootstrapper v2.

Reads session inputs, writes contract snapshot to .onex_state/, creates
required CronCreate jobs idempotently via CronList pre-check, and returns
a structured result. Runs FIRST each session.

v2 changes from v1:
- Actually calls CronCreate (v1 only returned timer_configs strings)
- Checks CronList before creating — no duplicate crons on re-run (C5 fix)
- Adds session_mode, active_sprint_id, model_routing_preference inputs
- crons_registered list in result replaces timer_configs
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from enum import StrEnum
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_session_bootstrap.models.model_session_contract import (
    ModelSessionContract,
)

logger = logging.getLogger(__name__)

_COST_CEILING_WARNING_THRESHOLD: float = 20.0

# Required crons for build mode (phase 1). merge_sweep + overseer_verify are phase 2.
_REQUIRED_CRONS_BUILD: list[dict[str, object]] = [
    {
        "cron_name": "build-dispatch-pulse",
        "interval_min": 30,
        "active_modes": ["build"],
        "prompt_template_key": "BUILD_DISPATCH_PULSE_PROMPT",
    },
]


class ModelBootstrapCommand(BaseModel):
    """Input command for the session bootstrap handler."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    contract: ModelSessionContract
    session_mode: str = "build"  # build | close-out | reporting
    active_sprint_id: str = "auto-detect"
    model_routing_preference: str = "local-first"
    state_dir: str = ".onex_state"
    dry_run: bool = False


class EnumBootstrapStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


class ModelBootstrapResult(BaseModel):
    """Result produced by HandlerSessionBootstrap."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    status: EnumBootstrapStatus
    contract_path: str
    crons_registered: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    dry_run: bool = False
    bootstrapped_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class HandlerSessionBootstrap:
    """Session bootstrap orchestrator v2.

    Creates required crons idempotently, writes contract snapshot, and returns
    ModelBootstrapResult. cron_create_fn and cron_list_fn are injected for testability.
    """

    def __init__(
        self,
        cron_create_fn: Callable[..., dict[str, object]] | None = None,
        cron_list_fn: Callable[[], list[dict[str, object]]] | None = None,
    ) -> None:
        self._cron_create = cron_create_fn
        self._cron_list = cron_list_fn

    def handle(self, command: ModelBootstrapCommand) -> ModelBootstrapResult:
        warnings: list[str] = []
        crons_registered: list[str] = []
        failed_cron_count = 0

        if not command.contract.phases_expected:
            warnings.append("phases_expected is empty — no phases will be tracked")
            logger.warning("Bootstrap: phases_expected is empty")

        if command.contract.cost_ceiling_usd > _COST_CEILING_WARNING_THRESHOLD:
            warnings.append(
                f"cost_ceiling_usd={command.contract.cost_ceiling_usd} exceeds "
                f"advisory threshold of {_COST_CEILING_WARNING_THRESHOLD}"
            )

        # Write contract snapshot (non-fatal on failure)
        contract_path = "(dry-run)"
        if not command.dry_run:
            try:
                contract_path = self._write_contract(command)
            except OSError as exc:
                warnings.append(f"contract snapshot write failed: {exc}")
                logger.warning("contract snapshot write failed: %s", exc)

        # Register required crons idempotently
        if not command.dry_run:
            crons_registered, failed_cron_count, cron_warnings = self._register_crons(command)
            warnings.extend(cron_warnings)
        else:
            for cron_def in _REQUIRED_CRONS_BUILD:
                if command.session_mode in cron_def["active_modes"]:  # type: ignore[operator]
                    crons_registered.append(f"(dry-run){cron_def['cron_name']}")

        # Severity-ranked status accumulator: failed > degraded > ready
        if failed_cron_count > 0 and not crons_registered:
            status = EnumBootstrapStatus.FAILED
        elif warnings:
            status = EnumBootstrapStatus.DEGRADED
        else:
            status = EnumBootstrapStatus.READY

        if not command.dry_run and crons_registered:
            try:
                self._write_cron_ids(command, crons_registered)
            except OSError as exc:
                warnings.append(f"cron ID log write failed: {exc}")
                if status == EnumBootstrapStatus.READY:
                    status = EnumBootstrapStatus.DEGRADED

        logger.info(
            "Bootstrap complete: session_id=%s status=%s crons=%s contract_path=%s",
            command.session_id, status.value, crons_registered, contract_path,
        )

        return ModelBootstrapResult(
            session_id=command.session_id,
            status=status,
            contract_path=contract_path,
            crons_registered=crons_registered,
            warnings=warnings,
            dry_run=command.dry_run,
        )

    def _register_crons(
        self, command: ModelBootstrapCommand
    ) -> tuple[list[str], int, list[str]]:
        """Create required crons, skipping any already registered. (C5 fix)"""
        warnings: list[str] = []
        crons_registered: list[str] = []
        failed_count = 0

        existing_names: set[str] = set()
        if self._cron_list is not None:
            try:
                existing = self._cron_list()
                existing_names = {
                    str(c.get("name", c.get("cron_name", ""))) for c in existing
                }
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"CronList failed — proceeding without dedup: {exc}")
                logger.warning("CronList failed: %s", exc)

        for cron_def in _REQUIRED_CRONS_BUILD:
            cron_name = str(cron_def["cron_name"])
            active_modes: list[str] = cron_def["active_modes"]  # type: ignore[assignment]
            if command.session_mode not in active_modes:
                continue

            if cron_name in existing_names:
                logger.info("cron already registered: %s (skipping CronCreate)", cron_name)
                warnings.append(f"cron already registered: {cron_name}")
                crons_registered.append(f"existing:{cron_name}")
                continue

            if self._cron_create is None:
                warnings.append(f"CronCreate not available — {cron_name} not registered")
                failed_count += 1
                continue

            try:
                interval_min = int(cron_def["interval_min"])  # type: ignore[arg-type]
                prompt = self._build_pulse_prompt(cron_name, command)
                result = self._cron_create(
                    cron=f"*/{interval_min} * * * *",
                    prompt=prompt,
                    recurring=True,
                )
                job_id = str(result.get("id", result.get("job_id", cron_name)))
                crons_registered.append(job_id)
                logger.info("cron created: name=%s job_id=%s", cron_name, job_id)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"CronCreate failed for {cron_name}: {exc}")
                logger.warning("CronCreate failed for %s: %s", cron_name, exc)
                failed_count += 1

        return crons_registered, failed_count, warnings

    def _build_pulse_prompt(self, cron_name: str, command: ModelBootstrapCommand) -> str:
        if cron_name == "build-dispatch-pulse":
            return (
                f"[Session {command.session_id}] Build dispatch pulse.\n"
                "FIRST ACTION: Read previous tick result from .onex_state/pulse-ticks/ (if any). "
                "Cross-check each claimed dispatched_task_id has a matching "
                ".onex_state/dispatch-events/{prev_tick_id}-{task_id}.json file. "
                "If any missing: log HALLUCINATED_PASS and emit health-violation event.\n\n"
                "1. Pull Linear active sprint tickets. Classify unworked tickets.\n"
                "2. For each unworked ticket: check if node_dispatch_worker is deployed and consuming "
                "onex.cmd.omnimarket.dispatch-worker-start.v1. If yes: route through it (dogfood path). "
                "If no: dispatch via Agent fallback.\n"
                "3. Write one .onex_state/dispatch-events/{tick_id}-{task_id}.json per dispatched ticket.\n"
                "4. VACUOUS_PULSE check: if dispatched==0 AND unworked_count>0: emit "
                "onex.evt.omnimarket.session-cron-health-violation.v1 with failure_class=VACUOUS_PULSE.\n"
                "5. Write tick result to .onex_state/pulse-ticks/{tick_id}.json with "
                "dispatched_task_ids, backlog_unworked_count, dispatch_path_used, verdict.\n"
            )
        return f"[Session {command.session_id}] {cron_name} tick."

    def _write_contract(self, command: ModelBootstrapCommand) -> str:
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

    def _write_cron_ids(self, command: ModelBootstrapCommand, cron_ids: list[str]) -> None:
        state_dir = os.path.abspath(command.state_dir)
        os.makedirs(state_dir, exist_ok=True)
        path = os.path.join(state_dir, f"session-crons-{command.session_id}.json")
        payload = {
            "session_id": command.session_id,
            "crons_registered": cron_ids,
            "written_at": datetime.now(tz=UTC).isoformat(),
        }
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2))


__all__: list[str] = [
    "EnumBootstrapStatus",
    "HandlerSessionBootstrap",
    "ModelBootstrapCommand",
    "ModelBootstrapResult",
]
