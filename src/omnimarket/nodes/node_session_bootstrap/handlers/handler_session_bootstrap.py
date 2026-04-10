# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerSessionBootstrap — Overnight session bootstrapper.

Reads ModelOvernightContract, writes contract snapshot to .onex_state/,
sets up timer configs, and emits session-bootstrap-completed event.
Runs FIRST each evening before any other overnight phase.

The handler is pure — it owns no external I/O beyond filesystem writes
controlled by the dry_run flag.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class EnumBootstrapStatus(StrEnum):
    """Bootstrap completion status."""

    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


class ModelBootstrapCommand(BaseModel, extra="forbid"):
    """Input command for HandlerSessionBootstrap."""

    session_id: str
    contract: dict[
        str, object
    ]  # ModelOvernightContract passed as dict for now (Task 1 adds the type)
    state_dir: str = ".onex_state"
    dry_run: bool = False


class ModelBootstrapResult(BaseModel, extra="forbid"):
    """Output result from HandlerSessionBootstrap."""

    session_id: str
    status: EnumBootstrapStatus
    contract_path: str
    timer_configs: list[str]
    warnings: list[str]
    dry_run: bool
    bootstrapped_at: datetime


class HandlerSessionBootstrap:
    """Bootstrap handler — skeleton stub (full implementation in Task 4)."""

    def handle(self, command: ModelBootstrapCommand) -> ModelBootstrapResult:
        """Bootstrap the overnight session."""
        warnings: list[str] = []

        # Validate contract basics
        phases_expected = command.contract.get("phases_expected", [])
        if not phases_expected:
            warnings.append("phases_expected is empty — session has no defined phases")

        cost_ceiling = command.contract.get("cost_ceiling_usd", 10.0)
        if isinstance(cost_ceiling, float | int) and cost_ceiling > 20.0:
            warnings.append(
                f"cost_ceiling_usd={cost_ceiling} exceeds recommended 20.0 advisory limit"
            )

        # Derive timer configs from phases
        timer_configs: list[str] = [
            "merge_sweep: every 20min",
            "health_check: every 10min",
            "agent_watchdog: every 5min",
        ]

        # Determine contract path
        if command.dry_run:
            contract_path = "(dry-run)"
        else:
            contract_path = (
                f"{command.state_dir}/overnight-contract-{command.session_id}.json"
            )
            # Full implementation (Task 4) will write the file here

        status = EnumBootstrapStatus.DEGRADED if warnings else EnumBootstrapStatus.READY

        return ModelBootstrapResult(
            session_id=command.session_id,
            status=status,
            contract_path=contract_path,
            timer_configs=timer_configs,
            warnings=warnings,
            dry_run=command.dry_run,
            bootstrapped_at=datetime.now(UTC),
        )
