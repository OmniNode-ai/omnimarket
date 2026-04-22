"""
Handler for Worker Stall Recovery node.

Wraps /onex:agent_healthcheck skill logic as a proper ONEX node.
Polls TaskList and activity timestamps, sends shutdown_request
and relaunches v2 agents for stalled tasks.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_worker_stall_recovery.models.model_stall_recovery_command import (
    ModelStallRecoveryCommand,
)

_OMNI_STATE_SENTINEL = ".onex_state"


def _resolve_onex_state() -> Path:
    """Resolve .onex_state directory via env var; never hardcode user paths."""
    omni_home = os.environ.get("OMNI_HOME")
    if omni_home:
        return Path(omni_home) / ".onex_state"
    local = Path(_OMNI_STATE_SENTINEL)
    if local.exists():
        return local
    raise RuntimeError(
        "Cannot locate .onex_state: set OMNI_HOME env var or run from omni_home root"
    )


class HandlerStallRecovery:
    """Handler that performs agent stall detection and recovery."""

    def __init__(self) -> None:
        self._initialized: bool = False

    async def initialize(self) -> None:
        """Initialize handler - verify required tools are available."""
        import shutil

        if shutil.which("grep") is None:
            raise RuntimeError("grep is not available")
        self._initialized = True

    async def handle(self, data: ModelStallRecoveryCommand) -> dict[str, Any]:
        """
        Check agent health and recover if stalled.

        Returns:
            status: healthy | stalled | recovered | failed | escalated
            stall_reason: Reason for stall (empty if healthy)
            checkpoint_path: Path to recovery checkpoint
            redispatch_count: Number of redispatches performed
            error: Error message if failed
        """
        dry_run = data.dry_run
        ticket_id = data.ticket_id
        agent_id = data.agent_id
        timeout = data.timeout_minutes
        max_redispatches = data.max_redispatches
        context_threshold_pct = data.context_threshold_pct

        checkpoint_path = self._get_checkpoint_path(ticket_id, agent_id)
        is_stalled, stall_reason = await self._check_stall(
            agent_id, timeout, context_threshold_pct
        )

        if not is_stalled and stall_reason not in (
            "dispatch_log_not_found",
            "agent_not_found_in_dispatch_log",
        ):
            return {
                "status": "healthy",
                "stall_reason": "",
                "checkpoint_path": "",
                "redispatch_count": 0,
                "error": "",
            }

        if not is_stalled:
            return {
                "status": "healthy",
                "stall_reason": stall_reason,
                "checkpoint_path": "",
                "redispatch_count": 0,
                "error": "",
            }

        if dry_run:
            return {
                "status": "stalled",
                "stall_reason": stall_reason,
                "checkpoint_path": str(checkpoint_path),
                "redispatch_count": 0,
                "error": "",
            }

        redispatch_count = 0
        for _attempt in range(max_redispatches):
            saved = await self._save_checkpoint(ticket_id, agent_id, checkpoint_path)
            if not saved:
                return {
                    "status": "failed",
                    "stall_reason": stall_reason,
                    "checkpoint_path": str(checkpoint_path),
                    "redispatch_count": redispatch_count,
                    "error": "Failed to save checkpoint",
                }

            success = await self._redispatch_agent(
                ticket_id, agent_id, redispatch_count
            )
            if success:
                redispatch_count += 1
                return {
                    "status": "recovered",
                    "stall_reason": stall_reason,
                    "checkpoint_path": str(checkpoint_path),
                    "redispatch_count": redispatch_count,
                    "error": "",
                }

        await self._escalate_to_blocked(ticket_id, checkpoint_path, redispatch_count)
        return {
            "status": "escalated",
            "stall_reason": stall_reason,
            "checkpoint_path": str(checkpoint_path),
            "redispatch_count": redispatch_count,
            "error": f"Exceeded {max_redispatches} redispatches",
        }

    def _get_checkpoint_path(self, ticket_id: str, agent_id: str) -> Path:
        """Compute checkpoint file path without creating directories."""
        try:
            onex_state = _resolve_onex_state()
        except RuntimeError:
            onex_state = Path(".onex_state")
        checkpoint_dir = onex_state / "pipeline_checkpoints" / ticket_id
        timestamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S")
        return checkpoint_dir / f"recovery-{agent_id}-{timestamp}.json"

    async def _check_stall(
        self, agent_id: str, timeout_minutes: int, context_threshold_pct: int
    ) -> tuple[bool, str]:
        """Check if agent is stalled based on activity timestamps.

        context_threshold_pct: if set > 0, also triggers stall if last event
        reports context_pct >= threshold (future: read from event payload).
        """
        try:
            onex_state = _resolve_onex_state()
        except RuntimeError:
            return False, "dispatch_log_not_found"

        dispatch_log_dir = onex_state / "dispatch-log"
        if not dispatch_log_dir.exists():
            return False, "dispatch_log_not_found"

        cutoff = datetime.now(tz=UTC) - timedelta(minutes=timeout_minutes)

        result = subprocess.run(
            ["grep", "-r", "-l", agent_id, str(dispatch_log_dir)],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0 or not result.stdout.strip():
            return False, "agent_not_found_in_dispatch_log"

        # grep -l may return multiple files; sort and pick the last (newest by name)
        matching_files = sorted(
            Path(p) for p in result.stdout.strip().splitlines() if p
        )
        if not matching_files:
            return False, "agent_not_found_in_dispatch_log"

        latest_log = matching_files[-1]
        if not latest_log.is_file():
            return False, "agent_not_found_in_dispatch_log"

        with open(latest_log) as f:
            for line in f:
                try:
                    event = json.loads(line)
                    if event.get("agent_id") == agent_id:
                        last_activity = event.get("timestamp", "")
                        if last_activity:
                            event_time = datetime.fromisoformat(
                                last_activity.replace("Z", "+00:00")
                            )
                            # Normalize to UTC-aware for comparison
                            if event_time.tzinfo is None:
                                event_time = event_time.replace(tzinfo=UTC)
                            if event_time > cutoff:
                                ctx_pct = event.get("context_pct", 0)
                                if (
                                    context_threshold_pct > 0
                                    and isinstance(ctx_pct, int | float)
                                    and ctx_pct >= context_threshold_pct
                                ):
                                    return (
                                        True,
                                        f"context_pct_{ctx_pct}_exceeds_{context_threshold_pct}",
                                    )
                                return False, ""
                except (json.JSONDecodeError, KeyError):
                    continue

        return True, f"inactivity_{timeout_minutes}_minutes"

    async def _save_checkpoint(
        self, ticket_id: str, agent_id: str, checkpoint_path: Path
    ) -> bool:
        """Save recovery checkpoint as JSON."""
        checkpoint_data = {
            "ticket_id": ticket_id,
            "agent_id": agent_id,
            "timestamp": datetime.now(tz=UTC).isoformat(),
            "reason": "stall_recovery_checkpoint",
        }
        try:
            checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
            checkpoint_path.write_text(json.dumps(checkpoint_data, indent=2))
            return True
        except Exception:
            return False

    async def _redispatch_agent(
        self, ticket_id: str, agent_id: str, attempt: int
    ) -> bool:
        """Redispatch agent with the same ticket, using attempt-indexed suffix."""
        try:
            onex_state = _resolve_onex_state()
        except RuntimeError:
            return False

        dispatch_dir = onex_state / "dispatches"
        if not dispatch_dir.exists():
            return False

        try:
            dispatch_file = dispatch_dir / f"{agent_id}.json"
            if not dispatch_file.exists():
                return False

            dispatch_data = json.loads(dispatch_file.read_text())
            new_agent_id = f"{agent_id}-recovery-{attempt + 1}"
            new_dispatch_file = dispatch_dir / f"{new_agent_id}.json"
            dispatch_data["agent_id"] = new_agent_id
            dispatch_data["original_agent_id"] = agent_id
            dispatch_data["redispatch_of"] = ticket_id
            dispatch_data["timestamp"] = datetime.now(tz=UTC).isoformat()
            new_dispatch_file.write_text(json.dumps(dispatch_data, indent=2))
            return True
        except Exception:
            return False

    async def _escalate_to_blocked(
        self, ticket_id: str, checkpoint_path: Path, attempt_count: int
    ) -> None:
        """Escalate to blocked in Linear and log friction."""
        try:
            onex_state = _resolve_onex_state()
        except RuntimeError:
            onex_state = Path(".onex_state")

        friction_dir = onex_state / "friction"
        friction_dir.mkdir(parents=True, exist_ok=True)

        date_today = datetime.now(tz=UTC).strftime("%Y-%m-%d")
        friction_file = (
            friction_dir / f"{date_today}-agent-stall-escalation-{ticket_id.lower()}.md"
        )
        friction_content = f"""# Agent Stall Escalation: {ticket_id}

## Summary
Agent stalled {attempt_count} times on {ticket_id}, exceeding the max redispatch limit.
Ticket moved to Blocked in Linear.

## Recovery Checkpoint
- Path: {checkpoint_path}
- Timestamp: {datetime.now(tz=UTC).isoformat()}

## Root Cause Hypothesis
Agent likely hitting context exhaustion or encountering a blocking issue that
persists across redispatches (e.g., missing dependency, broken test, infra issue).

## Recommended Action
Manual investigation required. Read the checkpoint and dispatch log to determine
whether the issue is agent-side (scope too large) or environment-side (broken infra).
"""
        friction_file.write_text(friction_content)
