# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerSessionBootstrap — Session bootstrapper.

Reads ModelSessionContract, writes a contract snapshot to .onex_state/,
derives timer configurations from expected phases, and returns a structured
result. Runs FIRST each session to initialize the session.

This handler is pure — no external I/O in the handler itself. Filesystem
writes are gated by dry_run. Callers pass an absolute state_dir path.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_session_bootstrap.models.model_session_contract import (
    ModelSessionContract,
)

logger = logging.getLogger(__name__)

# Timer configs always included regardless of phases
_DEFAULT_TIMERS: list[str] = [
    "merge_sweep (every 20min)",
    "health_check (every 10min)",
    "agent_watchdog (every 5min)",
]

# Advisory cost ceiling threshold for warnings
_COST_CEILING_WARNING_THRESHOLD: float = 20.0


class ModelBootstrapCommand(BaseModel):
    """Input command for the session bootstrap handler."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    contract: ModelSessionContract
    state_dir: str = ".onex_state"
    dry_run: bool = False


class EnumBootstrapStatus(StrEnum):
    """Terminal status for a bootstrap run."""

    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


class ModelBootstrapResult(BaseModel):
    """Result produced by HandlerSessionBootstrap."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    status: EnumBootstrapStatus
    contract_path: str
    timer_configs: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    dry_run: bool = False
    bootstrapped_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class HandlerSessionBootstrap:
    """Session bootstrap orchestrator.

    Pure handler — no direct I/O. Validates the session contract, derives
    timer configs from expected phases, writes contract JSON to disk (unless
    dry_run), and returns ModelBootstrapResult.
    """

    def handle(self, command: ModelBootstrapCommand) -> ModelBootstrapResult:
        """Execute session bootstrap.

        Args:
            command: Bootstrap command including session_id, contract, and flags.

        Returns:
            ModelBootstrapResult with status, contract_path, and timer_configs.
        """
        warnings: list[str] = []

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

        # Derive timer configs
        timer_configs = list(_DEFAULT_TIMERS)
        for phase in command.contract.phases_expected:
            phase_timer = f"{phase} (phase timer)"
            if phase_timer not in timer_configs:
                timer_configs.append(phase_timer)

        # Determine status
        status = EnumBootstrapStatus.DEGRADED if warnings else EnumBootstrapStatus.READY

        # Write contract to disk (unless dry_run)
        contract_path = "(dry-run)"
        if not command.dry_run:
            contract_path = self._write_contract(command)

        logger.info(
            "Bootstrap complete: session_id=%s status=%s contract_path=%s",
            command.session_id,
            status.value,
            contract_path,
        )

        return ModelBootstrapResult(
            session_id=command.session_id,
            status=status,
            contract_path=contract_path,
            timer_configs=timer_configs,
            warnings=warnings,
            dry_run=command.dry_run,
        )

    def _write_contract(self, command: ModelBootstrapCommand) -> str:
        """Write contract JSON to state_dir and return the absolute path."""
        state_dir = os.path.abspath(command.state_dir)
        os.makedirs(state_dir, exist_ok=True)
        filename = f"session-contract-{command.session_id}.json"
        path = os.path.join(state_dir, filename)
        payload = command.contract.model_dump()
        payload["session_id"] = command.session_id
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, indent=2, default=str))
        return path


__all__: list[str] = [
    "EnumBootstrapStatus",
    "HandlerSessionBootstrap",
    "ModelBootstrapCommand",
    "ModelBootstrapResult",
]
