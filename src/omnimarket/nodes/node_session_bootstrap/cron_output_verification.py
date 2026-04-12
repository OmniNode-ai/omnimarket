# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CronOutputVerificationRoutine — post-tick dispatch verification.

Runs inline inside the pulse tick (not a Kafka-subscribed handler).
Detects vacuous pulses (dispatched==0 and backlog>0) and maintains
a cross-tick verification chain to prevent hallucinated PASS results. (C1, C2 fix)

Promotes to a formal handler once the event pipeline is stable.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

logger = logging.getLogger(__name__)

_PULSE_TICKS_DIR = "pulse-ticks"
_DISPATCH_EVENTS_DIR = "dispatch-events"
_FRICTION_DIR = "friction"


@dataclass
class TickResult:
    tick_id: str
    dispatched_count: int
    dispatched_task_ids: list[str]
    backlog_unworked_count: int
    dispatch_path_used: str  # "dogfood" | "agent_bypass" | "mixed" | "none"
    verdict: Literal["pass", "fail"]
    failure_reason: str = ""


@dataclass
class VerificationInput:
    tick_id: str
    dispatched_task_ids: list[str]
    backlog_unworked_count: int
    dispatch_path_used: str
    dogfood_available: bool
    session_id: str
    previous_tick_result_path: str | None = None
    warnings: list[str] = field(default_factory=list)


class CronOutputVerificationRoutine:
    """Post-tick verification gate.

    Call verify() at the end of each pulse tick, after all dispatch attempts.
    Returns a TickResult. Writes tick result file for cross-tick validation.
    """

    def __init__(self, state_dir: str) -> None:
        self._state_dir = os.path.abspath(state_dir)

    def verify(self, inputs: VerificationInput) -> TickResult:
        # Gate 0: cross-check previous tick before anything else (C2 fix)
        self._cross_check_previous_tick(inputs)

        dispatched = len(inputs.dispatched_task_ids)

        # Gate 1: vacuous pulse
        if inputs.backlog_unworked_count > 0 and dispatched == 0:
            logger.warning(
                "VACUOUS_PULSE: tick=%s dispatched=0 unworked=%d",
                inputs.tick_id, inputs.backlog_unworked_count,
            )
            self._write_friction(inputs.tick_id, "vacuous-pulse", {
                "tick_id": inputs.tick_id,
                "backlog_unworked_count": inputs.backlog_unworked_count,
                "dispatch_path_used": inputs.dispatch_path_used,
            })
            result = TickResult(
                tick_id=inputs.tick_id,
                dispatched_count=0,
                dispatched_task_ids=[],
                backlog_unworked_count=inputs.backlog_unworked_count,
                dispatch_path_used=inputs.dispatch_path_used,
                verdict="fail",
                failure_reason=f"VACUOUS_PULSE: 0 dispatched, {inputs.backlog_unworked_count} unworked",
            )
            self._write_tick_result(result)
            return result

        # Gate 2: dogfood path check
        if inputs.dispatch_path_used == "agent_bypass":
            if inputs.dogfood_available:
                logger.warning(
                    "tick=%s: dispatch used Agent bypass — node_dispatch_worker was available",
                    inputs.tick_id,
                )
            else:
                logger.info("tick=%s: dispatch via Agent (node_dispatch_worker not deployed)", inputs.tick_id)
            self._append_bypass_log(inputs)

        # Gate 3: empty backlog — zero dispatch is acceptable
        if inputs.backlog_unworked_count == 0:
            result = TickResult(
                tick_id=inputs.tick_id,
                dispatched_count=dispatched,
                dispatched_task_ids=inputs.dispatched_task_ids,
                backlog_unworked_count=0,
                dispatch_path_used=inputs.dispatch_path_used,
                verdict="pass",
                failure_reason="",
            )
            self._write_tick_result(result)
            return result

        result = TickResult(
            tick_id=inputs.tick_id,
            dispatched_count=dispatched,
            dispatched_task_ids=inputs.dispatched_task_ids,
            backlog_unworked_count=inputs.backlog_unworked_count,
            dispatch_path_used=inputs.dispatch_path_used,
            verdict="pass",
            failure_reason="",
        )
        self._write_tick_result(result)
        return result

    def _cross_check_previous_tick(self, inputs: VerificationInput) -> None:
        """Validate previous tick's dispatch-event files exist for every claimed task_id."""
        prev_path = inputs.previous_tick_result_path
        if prev_path is None:
            return
        if not os.path.exists(prev_path):
            logger.error(
                "ESCALATE: previous tick result file missing (not first tick): %s — prev tick may have failed silently",
                prev_path,
            )
            inputs.warnings.append(f"previous tick result missing: {prev_path}")
            return

        try:
            with open(prev_path, encoding="utf-8") as fh:
                prev = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("failed to read previous tick result %s: %s", prev_path, exc)
            return

        if prev.get("verdict") == "pass" and prev.get("dispatched_task_ids"):
            prev_tick_id = prev.get("tick_id", "")
            events_dir = os.path.join(self._state_dir, _DISPATCH_EVENTS_DIR)
            for task_id in prev["dispatched_task_ids"]:
                expected = os.path.join(events_dir, f"{prev_tick_id}-{task_id}.json")
                if not os.path.exists(expected):
                    logger.error(
                        "HALLUCINATED PASS detected: prev tick=%s claimed task_id=%s but dispatch-event file missing: %s",
                        prev_tick_id, task_id, expected,
                    )
                    inputs.warnings.append(f"hallucinated_pass:prev_tick={prev_tick_id}:task_id={task_id}")
        elif prev.get("verdict") == "fail":
            logger.warning("previous tick %s was FAIL — increasing dispatch urgency", prev.get("tick_id"))
            inputs.warnings.append(f"prev_tick_failed:{prev.get('tick_id')}")

    def _write_tick_result(self, result: TickResult) -> None:
        ticks_dir = os.path.join(self._state_dir, _PULSE_TICKS_DIR)
        os.makedirs(ticks_dir, exist_ok=True)
        path = os.path.join(ticks_dir, f"{result.tick_id}.json")
        payload = {
            "tick_id": result.tick_id,
            "dispatched_count": result.dispatched_count,
            "dispatched_task_ids": result.dispatched_task_ids,
            "backlog_unworked_count": result.backlog_unworked_count,
            "dispatch_path_used": result.dispatch_path_used,
            "verdict": result.verdict,
            "failure_reason": result.failure_reason,
            "written_at": datetime.now(tz=UTC).isoformat(),
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        logger.debug("tick result written: %s verdict=%s", path, result.verdict)

    def _write_friction(self, tick_id: str, label: str, data: dict[str, object]) -> None:
        friction_dir = os.path.join(self._state_dir, _FRICTION_DIR)
        os.makedirs(friction_dir, exist_ok=True)
        ts = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S")
        path = os.path.join(friction_dir, f"{label}-{ts}.json")
        data["written_at"] = datetime.now(tz=UTC).isoformat()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)

    def _append_bypass_log(self, inputs: VerificationInput) -> None:
        log_path = os.path.join(self._state_dir, f"session-bypass-log-{inputs.session_id}.jsonl")
        entry = {
            "tick_id": inputs.tick_id,
            "dispatch_path_used": inputs.dispatch_path_used,
            "dogfood_available": inputs.dogfood_available,
            "timestamp": datetime.now(tz=UTC).isoformat(),
        }
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def find_latest_tick_result_path(self) -> str | None:
        """Return the path to the most recent tick result file, or None."""
        ticks_dir = os.path.join(self._state_dir, _PULSE_TICKS_DIR)
        if not os.path.isdir(ticks_dir):
            return None
        files = sorted(
            (f for f in os.listdir(ticks_dir) if f.endswith(".json")),
            reverse=True,
        )
        return os.path.join(ticks_dir, files[0]) if files else None


__all__: list[str] = [
    "CronOutputVerificationRoutine",
    "TickResult",
    "VerificationInput",
]
