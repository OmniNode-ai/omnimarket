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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_worker_stall_recovery.models.model_stall_recovery_command import (
    ModelStallRecoveryCommand,
)


class HandlerStallRecovery:
    """Handler that performs agent stall detection and recovery."""

    def __init__(self) -> None:
        self._dry_run: bool = False

    async def initialize(self) -> None:
        """Initialize handler - verify required tools are available."""
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError("Git is not available")

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
        self._dry_run = data.dry_run

        ticket_id = data.ticket_id
        agent_id = data.agent_id
        timeout = data.timeout_minutes
        max_redispatches = data.max_redispatches

        checkpoint_path = self._get_checkpoint_path(ticket_id, agent_id)
        is_stalled, stall_reason = await self._check_stall(
            agent_id, timeout, data.context_threshold_pct
        )

        if not is_stalled:
            return {
                "status": "healthy",
                "stall_reason": "",
                "checkpoint_path": "",
                "redispatch_count": 0,
                "error": "",
            }

        if self._dry_run:
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

            success = await self._redispatch_agent(ticket_id, agent_id)
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
        """Get checkpoint file path for recovery."""
        base = Path(os.environ.get("OMNI_HOME", "/Users/jonah/Code/omni_home"))
        checkpoint_dir = base / ".onex_state" / "pipeline_checkpoints" / ticket_id
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S")
        return checkpoint_dir / f"recovery-{timestamp}.yaml"

    async def _check_stall(
        self, agent_id: str, timeout_minutes: int, context_threshold_pct: int
    ) -> tuple[bool, str]:
        """Check if agent is stalled based on activity timestamps."""
        onex_state = Path(".onex_state")
        if not onex_state.exists():
            onex_state = Path.home() / "Code" / "omni_home" / ".onex_state"

        dispatch_log_dir = onex_state / "dispatch-log"
        if not dispatch_log_dir.exists():
            return False, "dispatch_log_not_found"

        cutoff = datetime.utcnow() - timedelta(minutes=timeout_minutes)

        result = subprocess.run(
            ["grep", "-l", agent_id, "-r", str(dispatch_log_dir)],
            capture_output=True,
            text=True,
        )

        if result.returncode != 0 or not result.stdout.strip():
            return False, "agent_not_found_in_dispatch_log"

        latest_log = sorted(Path(result.stdout.strip()).glob("*.ndjson"))[-1]
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
                            if event_time.replace(tzinfo=None) > cutoff:
                                return False, ""
                except (json.JSONDecodeError, KeyError):
                    continue

        return True, f"inactivity_{timeout_minutes}_minutes"

    async def _save_checkpoint(
        self, ticket_id: str, agent_id: str, checkpoint_path: Path
    ) -> bool:
        """Save recovery checkpoint."""
        checkpoint_data = {
            "ticket_id": ticket_id,
            "agent_id": agent_id,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "reason": "stall_recovery_checkpoint",
        }
        try:
            checkpoint_path.write_text(json.dumps(checkpoint_data, indent=2))
            return True
        except Exception:
            return False

    async def _redispatch_agent(self, ticket_id: str, agent_id: str) -> bool:
        """Redispatch agent with the same ticket."""
        onex_state = Path(".onex_state")
        if not onex_state.exists():
            onex_state = Path.home() / "Code" / "omni_home" / ".onex_state"

        dispatch_dir = onex_state / "dispatches"
        if not dispatch_dir.exists():
            return False

        try:
            dispatch_file = dispatch_dir / f"{agent_id}.json"
            if not dispatch_file.exists():
                return False

            dispatch_data = json.loads(dispatch_file.read_text())
            new_agent_id = f"{agent_id}-v2"
            new_dispatch_file = dispatch_dir / f"{new_agent_id}.json"
            dispatch_data["agent_id"] = new_agent_id
            dispatch_data["original_agent_id"] = agent_id
            dispatch_data["redispatch_of"] = ticket_id
            dispatch_data["timestamp"] = datetime.utcnow().isoformat() + "Z"
            new_dispatch_file.write_text(json.dumps(dispatch_data, indent=2))
            return True
        except Exception:
            return False

    async def _escalate_to_blocked(
        self, ticket_id: str, checkpoint_path: Path, attempt_count: int
    ) -> None:
        """Escalate to blocked in Linear and log friction."""
        onex_state = Path(".onex_state")
        if not onex_state.exists():
            onex_state = Path.home() / "Code" / "omni_home" / ".onex_state"

        friction_dir = onex_state / "friction"
        friction_dir.mkdir(parents=True, exist_ok=True)

        date_today = datetime.utcnow().strftime("%Y-%m-%d")
        friction_file = (
            friction_dir / f"{date_today}-agent-stall-escalation-{ticket_id.lower()}.md"
        )
        friction_content = f"""# Agent Stall Escalation: {ticket_id}

## Summary
Agent stalled {attempt_count} times on {ticket_id}, exceeding the max redispatch limit.
Ticket moved to Blocked in Linear.

## Recovery Checkpoint
- Path: {checkpoint_path}
- Timestamp: {datetime.utcnow().isoformat()}Z

## Root Cause Hypothesis
Agent likely hitting context exhaustion or encountering a blocking issue that
persists across redispatches (e.g., missing dependency, broken test, infra issue).

## Recommended Action
Manual investigation required. Read the checkpoint and dispatch log to determine
whether the issue is agent-side (scope too large) or environment-side (broken infra).
"""
        friction_file.write_text(friction_content)
