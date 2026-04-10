# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerSessionBootstrap — Overnight session bootstrapper.

Reads ModelOvernightContract, writes contract snapshot to .onex_state/,
sets up timer configs, and emits session-bootstrap-completed event.
Runs FIRST each evening before any other overnight phase.

The handler is pure — filesystem writes are controlled by the dry_run flag.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel

logger = logging.getLogger(__name__)

_TIMER_CONFIGS: list[str] = [
    "merge_sweep: every 20min",
    "health_check: every 10min",
    "agent_watchdog: every 5min",
]


class EnumBootstrapStatus(StrEnum):
    """Bootstrap completion status."""

    READY = "ready"
    DEGRADED = "degraded"
    FAILED = "failed"


class ModelBootstrapCommand(BaseModel, extra="forbid"):
    """Input command for HandlerSessionBootstrap."""

    session_id: str
    contract: dict[str, object]  # ModelOvernightContract serialized as dict
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
    """Overnight session bootstrapper.

    Validates the overnight contract, writes a snapshot to .onex_state/,
    and returns timer configurations for the session. Pure FSM — all I/O
    is guarded by the dry_run flag.
    """

    def handle(self, command: ModelBootstrapCommand) -> ModelBootstrapResult:
        """Bootstrap the overnight session.

        Args:
            command: Bootstrap command with contract dict and session ID.

        Returns:
            ModelBootstrapResult with status, contract path, and timer configs.
        """
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

        # Determine contract path
        if command.dry_run:
            contract_path = "(dry-run)"
        else:
            abs_state_dir = os.path.abspath(command.state_dir)
            os.makedirs(abs_state_dir, exist_ok=True)
            contract_path = os.path.join(
                abs_state_dir, f"overnight-contract-{command.session_id}.json"
            )
            contract_payload = {
                "session_id": command.session_id,
                "written_at": datetime.now(UTC).isoformat(),
                "contract": command.contract,
            }
            with open(contract_path, "w") as f:
                json.dump(contract_payload, f, indent=2)
            logger.info("Wrote contract snapshot to %s", contract_path)

        status = EnumBootstrapStatus.DEGRADED if warnings else EnumBootstrapStatus.READY

        return ModelBootstrapResult(
            session_id=command.session_id,
            status=status,
            contract_path=contract_path,
            timer_configs=list(_TIMER_CONFIGS),
            warnings=warnings,
            dry_run=command.dry_run,
            bootstrapped_at=datetime.now(UTC),
        )


__all__: list[str] = [
    "EnumBootstrapStatus",
    "HandlerSessionBootstrap",
    "ModelBootstrapCommand",
    "ModelBootstrapResult",
]
