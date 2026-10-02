# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Recover the one held terminal of overdue delegation commands."""

from __future__ import annotations

import logging
from datetime import datetime

from omnibase_core.models.delegation.wire import EnumDelegationTerminalFailureCause
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_delegation_terminal_record import (
    terminal_from_record,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_handler_execution_budget import (
    ModelDelegationReaperConfig,
    load_delegation_reaper_config,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_delegation_claim import (
    ProtocolDelegationReaperPort,
    resolve_delegation_claim_store,
)

logger = logging.getLogger(__name__)


class HandlerDelegationReaper:
    """Runtime ticks close abandoned commands and heal interrupted handoffs."""

    handler_id = "delegate-skill.reap_scheduled_run"

    def __init__(
        self,
        *,
        port: ProtocolDelegationReaperPort | None = None,
        config: ModelDelegationReaperConfig | None = None,
    ) -> None:
        self._port = port
        self._config = config if config is not None else load_delegation_reaper_config()
        self._last_scan: datetime | None = None

    async def handle(self, tick: ModelRuntimeTick) -> ModelHandlerOutput[None] | None:
        """Publish slot winners, preserving a held real terminal during healing."""
        if self._port is None:
            self._port = resolve_delegation_claim_store()
        port = self._port
        now = tick.now
        if (
            self._last_scan is not None
            and (now - self._last_scan).total_seconds()
            < self._config.scan_interval_seconds
        ):
            return None
        self._last_scan = now
        stalled = port.stalled_claims(now=now, limit=self._config.max_reaps_per_tick)
        terminals: list[ModelDelegateSkillCompleted | ModelDelegateSkillFailed] = []
        won = healed = failed = unreadable = 0
        for claim in stalled:
            try:
                ctx = claim.context
                terminal = ModelDelegateSkillFailed(
                    status="failed",
                    correlation_id=ctx.correlation_id,
                    task_type=ctx.task_type,
                    tenant_id=ctx.tenant_id,
                    provenance=ctx.provenance,
                    ticket_id=ctx.ticket_id,
                    caller_lane=ctx.caller_lane,
                    session_id=ctx.session_id,
                    terminal_failure_cause=EnumDelegationTerminalFailureCause.NO_TERMINAL,
                    error_message=(
                        f"Claimed at {claim.claimed_at.isoformat()}, no terminal by "
                        f"{ctx.deadline_at.isoformat()}, closed by the delegation reaper; "
                        "the command may or may not have run, its late result is kept "
                        "as attempt evidence."
                    ),
                    execution_duration_ms=max(
                        0, int((now - claim.claimed_at).total_seconds() * 1000)
                    ),
                )
                outcome = port.reap(
                    delivery_id=claim.delivery_id,
                    terminal={
                        "cls": type(terminal).__name__,
                        "data": terminal.model_dump(mode="json"),
                    },
                )
                held = terminal_from_record(outcome.terminal)
                if held is None:
                    unreadable += 1
                    continue
                terminals.append(held)
                won += int(outcome.won)
                healed += int(not outcome.won)
            except Exception:
                failed += 1
                logger.exception(
                    "Delegation reaper could not hand off command %s", claim.delivery_id
                )
        if not terminals:
            return None
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=tick.tick_id,
            correlation_id=tick.correlation_id or tick.tick_id,
            handler_id=self.handler_id,
            events=tuple(terminals),
            metrics={
                "stalled_count": float(len(stalled)),
                "reaped_count": float(won),
                "healed_count": float(healed),
                "failed_count": float(failed),
                "unreadable_count": float(unreadable),
            },
        )
