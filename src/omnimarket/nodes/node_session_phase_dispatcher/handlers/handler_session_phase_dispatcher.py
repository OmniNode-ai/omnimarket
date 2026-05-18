# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Session phase dispatcher effect handler.

Receives phase transition commands and publishes phase-state and budget-warning
events. This is the ONLY node in the session-phase subsystem with side effects.
Topics are read from contract.yaml — never hardcoded.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import yaml

from omnimarket.nodes.node_session_phase_dispatcher.models.model_dispatcher_input import (
    ModelSessionPhaseDispatcherInput,
    ModelSessionPhaseTransitionCommand,
)
from omnimarket.nodes.node_session_phase_dispatcher.models.model_dispatcher_result import (
    ModelSessionPhaseDispatcherResult,
    ModelSessionPhaseEvent,
)

logger = logging.getLogger(__name__)

HandlerType = Literal["node_handler"]
HandlerCategory = Literal["effect"]

_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
_BUDGET_WARNING_THRESHOLD = 0.8  # warn at 80% budget consumed


def _load_publish_topics() -> dict[str, str]:
    """Load publish topic names from contract.yaml."""
    if not _CONTRACT_PATH.exists():
        msg = f"contract.yaml not found at {_CONTRACT_PATH}"
        raise RuntimeError(msg)
    with _CONTRACT_PATH.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    topics: list[str] = (data.get("event_bus", {}) or {}).get(
        "publish_topics", []
    ) or []
    result: dict[str, str] = {}
    for topic in topics:
        if "session-phase-state" in topic:
            result["phase_state"] = topic
        elif "session-phase-budget-warning" in topic:
            result["budget_warning"] = topic
    required = {"phase_state", "budget_warning"}
    missing = required - result.keys()
    if missing:
        msg = f"contract.yaml missing publish topics: {missing}"
        raise RuntimeError(msg)
    return result


_TOPICS = _load_publish_topics()
_TOPIC_PHASE_STATE = _TOPICS["phase_state"]
_TOPIC_BUDGET_WARNING = _TOPICS["budget_warning"]

_EVENT_TYPE_PHASE_STATE = "omnimarket.session-phase-state"
_EVENT_TYPE_BUDGET_WARNING = "omnimarket.session-phase-budget-warning"


class HandlerSessionPhaseDispatcher:
    """Publish phase transition events and dispatch workers from phase specs."""

    @property
    def handler_type(self) -> HandlerType:
        return "node_handler"

    @property
    def handler_category(self) -> HandlerCategory:
        return "effect"

    def handle(
        self, command: ModelSessionPhaseDispatcherInput
    ) -> ModelSessionPhaseDispatcherResult:
        all_events: list[ModelSessionPhaseEvent] = []
        total_workers = 0
        total_warnings = 0

        for cmd in command.commands:
            events, workers, warnings = self._process_transition(cmd)
            all_events.extend(events)
            total_workers += workers
            total_warnings += warnings

        # Use correlation_id from first command as the result correlation
        return ModelSessionPhaseDispatcherResult(
            correlation_id=command.commands[0].correlation_id,
            events=tuple(all_events),
            workers_dispatched=total_workers,
            budget_warnings_emitted=total_warnings,
        )

    def _process_transition(
        self, cmd: ModelSessionPhaseTransitionCommand
    ) -> tuple[list[ModelSessionPhaseEvent], int, int]:
        events: list[ModelSessionPhaseEvent] = []
        workers_dispatched = 0
        warnings_emitted = 0

        # Always publish phase-state event
        events.append(
            ModelSessionPhaseEvent(
                topic=_TOPIC_PHASE_STATE,
                event_type=_EVENT_TYPE_PHASE_STATE,
                payload={
                    "session_id": cmd.session_id,
                    "phase_name": cmd.phase_name,
                    "transition": cmd.transition,
                    "correlation_id": str(cmd.correlation_id),
                    "elapsed_seconds": cmd.elapsed_seconds,
                    "cost_usd": cmd.cost_usd,
                },
            )
        )

        # Dispatch workers from phase spec dispatch_items on "enter"
        if cmd.transition == "enter" and cmd.phase_spec is not None:
            for item in cmd.phase_spec.dispatch_items:
                logger.info(
                    "Dispatching worker for phase=%s theme=%s",
                    cmd.phase_name,
                    item.theme_id,
                )
                workers_dispatched += 1

        # Emit budget warning when cost crosses threshold
        if (
            cmd.budget_usd > 0
            and (cmd.cost_usd / cmd.budget_usd) >= _BUDGET_WARNING_THRESHOLD
        ):
            events.append(
                ModelSessionPhaseEvent(
                    topic=_TOPIC_BUDGET_WARNING,
                    event_type=_EVENT_TYPE_BUDGET_WARNING,
                    payload={
                        "session_id": cmd.session_id,
                        "phase_name": cmd.phase_name,
                        "cost_usd": cmd.cost_usd,
                        "budget_usd": cmd.budget_usd,
                        "pct_consumed": round(cmd.cost_usd / cmd.budget_usd * 100, 1),
                        "correlation_id": str(cmd.correlation_id),
                    },
                )
            )
            warnings_emitted += 1

        return events, workers_dispatched, warnings_emitted


__all__ = ["HandlerSessionPhaseDispatcher"]
