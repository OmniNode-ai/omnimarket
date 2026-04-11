# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Overseer tick helpers — halt_condition evaluation, required_outcome probes,
and `.onex_state/overseer-active.flag` writer.

OMN-8375 adds the declarative halt-condition + context re-injection layer on
top of the HandlerOvernight phase FSM. This module stays pure so it can be
unit-tested without booting the full handler.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from omnibase_compat.overseer.model_overnight_contract import (
    ModelOvernightContract,
    ModelOvernightHaltCondition,
)

from omnimarket.nodes.node_overnight.topics import TOPIC_OVERSEER_TICK

logger = logging.getLogger(__name__)

# Probe returns True when the outcome is satisfied.
OutcomeProbe = Callable[[str], bool]

# Tick emitter — invoked once per phase tick with a JSON-serializable snapshot.
TickEmitter = Callable[[dict[str, Any]], None]

# Halt action — invoked when an on_halt condition fires. Returns True if the
# action resolved the condition (pipeline can continue), False to stop.
HaltActionHandler = Callable[[ModelOvernightHaltCondition, dict[str, Any]], bool]


OVERSEER_FLAG_PATH = Path(".onex_state/overseer-active.flag")
OVERSEER_TICK_LOG = Path(".onex_state/overseer-ticks.jsonl")
# Re-exported alias for the contract-declared topic. The source of truth lives
# in node_overnight/topics.py (and contract.yaml publish_topics). Callers that
# previously imported OVERSEER_TICK_TOPIC from this module keep working.
OVERSEER_TICK_TOPIC = TOPIC_OVERSEER_TICK


def resolve_state_root(explicit: Path | None = None) -> Path:
    """Return the directory that hosts `.onex_state/` for this run.

    Honors ``OMNI_HOME`` so background sessions write to the shared state
    directory and not their transient cwd. Tests pass ``explicit`` to isolate.
    """
    if explicit is not None:
        return explicit
    env = os.environ.get("OMNI_HOME")
    if env:
        return Path(env)
    return Path.cwd()


def write_overseer_flag(
    *,
    contract_path: Path | str | None,
    current_phase: str,
    session_id: str,
    started_at: datetime,
    snapshot: dict[str, Any],
    state_root: Path | None = None,
) -> Path:
    """Write the overseer-active flag so the PreToolUse hook can see it.

    The YAML shape is frozen by OMN-8376's hook contract:
        contract_path: str
        active_phase: str
        started_at: ISO8601

    We append the full snapshot under ``snapshot:`` so agents (SessionStart
    / UserPromptSubmit hooks) can re-inject current state without replaying
    the entire tick log.
    """
    root = resolve_state_root(state_root)
    flag_path = root / OVERSEER_FLAG_PATH
    flag_path.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "contract_path": str(contract_path) if contract_path is not None else "",
        "active_phase": current_phase,
        "session_id": session_id,
        "started_at": started_at.isoformat(),
        "updated_at": datetime.now(tz=UTC).isoformat(),
        "snapshot": snapshot,
    }
    flag_path.write_text(yaml.safe_dump(payload, sort_keys=True))
    return flag_path


def remove_overseer_flag(state_root: Path | None = None) -> None:
    """Remove the overseer-active flag on pipeline completion or halt."""
    flag_path = resolve_state_root(state_root) / OVERSEER_FLAG_PATH
    if flag_path.exists():
        flag_path.unlink()


def append_tick_log(
    snapshot: dict[str, Any],
    state_root: Path | None = None,
) -> None:
    """Append a tick snapshot to .onex_state/overseer-ticks.jsonl.

    Parallels the event emission step — gives observers a local artifact
    when no Kafka publisher is wired. Matches the shape that would be
    published to ``onex.evt.omnimarket.overseer.tick.v1``.
    """
    root = resolve_state_root(state_root)
    log_path = root / OVERSEER_TICK_LOG
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as fh:
        fh.write(json.dumps(snapshot, default=str) + "\n")


def build_tick_snapshot(
    *,
    contract: ModelOvernightContract,
    contract_path: Path | str | None,
    current_phase: str,
    phase_progress: float,
    phase_outcomes: dict[str, bool],
    accumulated_cost: float,
    started_at: datetime,
) -> dict[str, Any]:
    """Build a tick snapshot dict ready for flag write, log append, or event emit.

    Fields match the contract of ``onex.evt.omnimarket.overseer.tick.v1``:
      - contract_path
      - current_phase
      - phase_progress
      - next_required_outcome
      - approaching_halt_conditions
    """
    next_missing = next(
        (name for name, ok in phase_outcomes.items() if not ok),
        None,
    )

    approaching: list[dict[str, Any]] = []
    # Report cost-ceiling proximity as a percentage. Other check types are
    # summarized by check_type + on_halt routing so observers see them
    # without needing to re-read the contract.
    for cond in _iter_all_halt_conditions(contract, current_phase):
        entry: dict[str, Any] = {
            "condition_id": cond.condition_id,
            "check_type": cond.check_type,
            "on_halt": cond.on_halt,
        }
        if cond.check_type == "cost_ceiling" and cond.threshold > 0:
            entry["proximity_pct"] = min(
                100.0, (accumulated_cost / cond.threshold) * 100.0
            )
        if cond.skill:
            entry["skill"] = cond.skill
        approaching.append(entry)

    return {
        "topic": OVERSEER_TICK_TOPIC,
        "contract_path": str(contract_path) if contract_path is not None else "",
        "session_id": contract.session_id,
        "current_phase": current_phase,
        "phase_progress": round(phase_progress, 3),
        "phase_outcomes": phase_outcomes,
        "next_required_outcome": next_missing,
        "approaching_halt_conditions": approaching,
        "accumulated_cost": accumulated_cost,
        "started_at": started_at.isoformat(),
        "emitted_at": datetime.now(tz=UTC).isoformat(),
    }


def _iter_all_halt_conditions(
    contract: ModelOvernightContract,
    current_phase: str,
) -> list[ModelOvernightHaltCondition]:
    """Return contract-level + current-phase-scoped halt conditions."""
    items: list[ModelOvernightHaltCondition] = list(contract.halt_conditions)
    for spec in contract.phases:
        if spec.phase_name == current_phase:
            items.extend(spec.halt_conditions)
    return items


def probe_required_outcomes(
    outcomes: tuple[str, ...],
    probe: OutcomeProbe | None,
) -> dict[str, bool]:
    """Probe each required outcome; unresolvable names default to False.

    When probe is None, every outcome is reported as unsatisfied so the
    caller treats the phase as incomplete — phases must never silently
    advance past outcomes with no probe wired.
    """
    result: dict[str, bool] = {}
    for outcome in outcomes:
        if probe is None:
            result[outcome] = False
            continue
        try:
            result[outcome] = bool(probe(outcome))
        except Exception as exc:
            logger.warning("[OVERSEER] outcome probe %s raised: %s", outcome, exc)
            result[outcome] = False
    return result


def evaluate_halt_conditions(
    *,
    contract: ModelOvernightContract,
    current_phase: str,
    phase_outcomes: dict[str, bool],
    accumulated_cost: float,
    consecutive_failures: int,
    phase_started_at: datetime,
) -> list[tuple[ModelOvernightHaltCondition, str]]:
    """Return list of (condition, reason) for every halt condition that triggered.

    The handler decides what to do with each entry based on its ``on_halt``.
    This function is pure — it does not mutate state or dispatch anything.
    """
    now = datetime.now(tz=UTC)
    triggered: list[tuple[ModelOvernightHaltCondition, str]] = []

    for cond in _iter_all_halt_conditions(contract, current_phase):
        reason = _evaluate_one(
            cond=cond,
            phase=current_phase,
            phase_outcomes=phase_outcomes,
            accumulated_cost=accumulated_cost,
            consecutive_failures=consecutive_failures,
            phase_started_at=phase_started_at,
            now=now,
        )
        if reason is not None:
            triggered.append((cond, reason))

    return triggered


def _evaluate_one(
    *,
    cond: ModelOvernightHaltCondition,
    phase: str,
    phase_outcomes: dict[str, bool],
    accumulated_cost: float,
    consecutive_failures: int,
    phase_started_at: datetime,
    now: datetime,
) -> str | None:
    if cond.check_type == "cost_ceiling":
        if accumulated_cost >= cond.threshold:
            return f"cost_ceiling: {accumulated_cost:.2f} >= {cond.threshold:.2f} USD"
        return None

    if cond.check_type == "phase_failure_count":
        if consecutive_failures >= int(cond.threshold):
            return (
                f"phase_failure_count: {consecutive_failures} >= {int(cond.threshold)}"
            )
        return None

    if cond.check_type == "time_elapsed":
        elapsed = (now - phase_started_at).total_seconds()
        if elapsed >= cond.threshold:
            return f"time_elapsed: {elapsed:.0f}s >= {cond.threshold:.0f}s"
        return None

    if cond.check_type == "pr_blocked_too_long":
        if cond.pr is None or cond.threshold_minutes is None:
            return None
        minutes_blocked = _pr_blocked_minutes(cond.pr)
        if minutes_blocked is None:
            return None
        if minutes_blocked >= cond.threshold_minutes:
            return (
                f"pr_blocked_too_long: PR #{cond.pr} blocked "
                f"{minutes_blocked:.1f}m >= {cond.threshold_minutes:.1f}m"
            )
        return None

    if cond.check_type == "required_outcome_missing":
        if cond.outcome is None:
            return None
        # Only flag when the outcome was probed and came back False.
        if phase_outcomes.get(cond.outcome, True) is False:
            return f"required_outcome_missing: {cond.outcome} not satisfied in {phase}"
        return None

    # custom / unknown → caller must evaluate externally
    return None


def _pr_blocked_minutes(pr_number: int) -> float | None:
    """Return how many minutes the PR has been in a blocked mergeable state.

    Uses ``gh`` to avoid wiring a new GitHub client. Returns None when the
    PR cannot be queried, isn't blocked, or gh is unavailable — callers
    treat None as "cannot evaluate" and skip the condition.

    The blocked-since timestamp is derived from the most recent
    ``review_requested`` or ``convert_to_draft`` timeline event rather than
    ``updatedAt``, which moves on every comment and would produce incorrect
    durations.
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--json",
                "mergeStateStatus,state,statusCheckRollup,timelineItems",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("[OVERSEER] gh pr view %s failed: %s", pr_number, exc)
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if data.get("mergeStateStatus") != "BLOCKED":
        return None

    # Walk timeline items in reverse to find the most recent event that
    # caused the BLOCKED state. ``review_requested`` and ``convert_to_draft``
    # are the canonical state-transition events; fall back to the most recent
    # failed status check's ``completedAt`` if neither is present.
    blocked_since: datetime | None = None
    for item in reversed(data.get("timelineItems", [])):
        event_type = item.get("__typename", "")
        if event_type in ("ReviewRequestedEvent", "ConvertToDraftEvent"):
            raw = item.get("createdAt") or item.get("updatedAt")
            if isinstance(raw, str):
                try:
                    blocked_since = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                    break
                except ValueError:
                    continue

    if blocked_since is None:
        # Fallback: most recent failed status check completion time.
        for check in data.get("statusCheckRollup", []):
            if check.get("conclusion") in ("FAILURE", "TIMED_OUT", "ACTION_REQUIRED"):
                raw = check.get("completedAt")
                if isinstance(raw, str):
                    try:
                        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                        if blocked_since is None or ts > blocked_since:
                            blocked_since = ts
                    except ValueError:
                        continue

    if blocked_since is None:
        return None

    delta = datetime.now(tz=UTC) - blocked_since
    return delta.total_seconds() / 60.0


__all__ = [
    "OVERSEER_FLAG_PATH",
    "OVERSEER_TICK_LOG",
    "OVERSEER_TICK_TOPIC",
    "HaltActionHandler",
    "OutcomeProbe",
    "TickEmitter",
    "append_tick_log",
    "build_tick_snapshot",
    "evaluate_halt_conditions",
    "probe_required_outcomes",
    "remove_overseer_flag",
    "resolve_state_root",
    "write_overseer_flag",
]
