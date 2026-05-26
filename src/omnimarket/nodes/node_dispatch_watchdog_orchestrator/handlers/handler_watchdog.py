# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDispatchWatchdogOrchestrator — Epic-level wave stall monitor.

ONEX node type: ORCHESTRATOR — impure, effectful.

Wave 1: contract + stub only.  Full implementation deferred to Wave 2 (OMN-12209).
The handler class is importable and passes type checks; `handle()` raises
NotImplementedError as declared by `node_not_implemented: true` in contract.yaml.

Algorithm (per dispatch_watchdog SKILL.md):
  1. For each active task in the wave, call TaskGet() to read last_activity timestamp.
  2. Compute elapsed seconds since last tool call.
  3. Apply Bash long-timeout exemption: if last_tool_name == "Bash" and
     last_tool_timeout_ms > 120000, extend threshold to (timeout_ms/1000 + 60s).
  4. If elapsed > effective_timeout: stall_detected.
     - action == "report"     → log only.
     - action == "cancel"     → SendMessage shutdown_request.
     - action == "redispatch" → kill + redispatch with narrower scope.
  5. If redispatch_count >= max_redispatches: escalate to Blocked in Linear,
     write friction event to .onex_state/friction/, log to dispatch-log.
  6. Write watchdog.json + append to dispatch-log/{date}.ndjson.
  7. Emit ModelWatchdogResult.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_dispatch_watchdog_orchestrator.models.model_watchdog import (
    EnumRecoveryAction,
    ModelWatchdogResult,
    ModelWaveTask,
)

# ---------------------------------------------------------------------------
# Request model (lives here so contract.yaml input_model path is canonical)
# ---------------------------------------------------------------------------

_DEFAULT_CHECK_INTERVAL = 30
_DEFAULT_STALL_TIMEOUT = 120
_DEFAULT_MAX_REDISPATCHES = 2


class ModelWatchdogRequest(BaseModel):
    """Input envelope for the dispatch watchdog orchestrator."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    epic_id: str | None = Field(
        default=None,
        description=(
            "Epic ID to monitor. When set, the watchdog reads wave state from "
            "$ONEX_STATE_DIR/epics/<epic_id>/state.yaml. If None, wave_tasks must "
            "be provided directly."
        ),
    )
    wave_tasks: tuple[ModelWaveTask, ...] = Field(
        default=(),
        description=(
            "Explicit list of wave tasks to monitor. Used when epic_id is not set "
            "or when the caller injects task state directly."
        ),
    )
    check_interval_seconds: int = Field(
        default=_DEFAULT_CHECK_INTERVAL,
        ge=5,
        description="Polling interval in seconds between watchdog checks.",
    )
    stall_timeout_seconds: int = Field(
        default=_DEFAULT_STALL_TIMEOUT,
        ge=10,
        description=(
            "Inactivity threshold in seconds before a task is declared stalled. "
            "Extended automatically for long-running Bash calls."
        ),
    )
    max_redispatches: int = Field(
        default=_DEFAULT_MAX_REDISPATCHES,
        ge=1,
        description="Max redispatch attempts per task before escalation to Blocked.",
    )
    action: EnumRecoveryAction = Field(
        default=EnumRecoveryAction.REDISPATCH,
        description="Recovery action to take on stall detection.",
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "When True, log stall events but do not kill/redispatch agents or "
            "mutate Linear state."
        ),
    )


# ---------------------------------------------------------------------------
# Handler stub
# ---------------------------------------------------------------------------


class HandlerDispatchWatchdogOrchestrator:
    """ORCHESTRATOR — epic-level wave stall monitor and recovery dispatcher.

    Wave 1 contract-first node: importable and type-safe.  Full implementation
    in Wave 2 (OMN-12209).

    Per contract.yaml `node_not_implemented: true`, `handle()` raises
    NotImplementedError.  Callers should check the contract flag before invoking.
    """

    def handle(self, request: ModelWatchdogRequest) -> ModelWatchdogResult:  # stub-ok
        """Run one watchdog check pass over the wave tasks.

        Raises:
            NotImplementedError: contract.yaml node_not_implemented=true, Wave 2 in OMN-12209.
        """
        raise NotImplementedError(  # stub-ok
            "node_dispatch_watchdog_orchestrator is a Wave 1 contract-first node. "
            "Full implementation is tracked in OMN-12209 Wave 2. "
            "See contract.yaml `node_not_implemented: true`."
        )
