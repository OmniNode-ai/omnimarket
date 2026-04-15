# SPDX-License-Identifier: MIT
"""Handler that captures session state and writes a handoff artifact.

Deterministic git-state capture via subprocess. No LLM calls.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import yaml

logger = logging.getLogger(__name__)

_SSH_HOST = "jonah@192.168.86.201"
_SSH_TIMEOUT = 15


class InfraHealthGatherError(RuntimeError):
    """Raised when a mandatory infra health check cannot be sourced."""


def _run(args: list[str], cwd: str) -> str:
    try:
        result = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError) as exc:
        logger.warning("subprocess call failed %s: %s", args, exc)
        return ""


def _ssh(remote_cmd: str, check_name: str) -> str:
    """Run a remote SSH command. Raises InfraHealthGatherError on any failure."""
    try:
        result = subprocess.run(
            [
                "ssh",
                "-o",
                "ConnectTimeout=10",
                "-o",
                "BatchMode=yes",
                _SSH_HOST,
                remote_cmd,
            ],
            capture_output=True,
            text=True,
            timeout=_SSH_TIMEOUT,
        )
        output = result.stdout.strip()
        if not output and result.returncode != 0:
            raise InfraHealthGatherError(
                f"SSH check '{check_name}' returned exit {result.returncode}: {result.stderr.strip()}"
            )
        return output
    except subprocess.TimeoutExpired as exc:
        raise InfraHealthGatherError(
            f"SSH check '{check_name}' timed out after {_SSH_TIMEOUT}s"
        ) from exc
    except OSError as exc:
        raise InfraHealthGatherError(f"SSH check '{check_name}' failed: {exc}") from exc


def _parse_env_sync_log(log_path: Path) -> dict[str, str]:
    """Parse env-sync.log for last SUCCESS and last run line."""
    if not log_path.exists():
        return {
            "seed_infisical_last_success": "NEVER",
            "seed_infisical_last_run": "unknown (log not found)",
        }

    lines = [
        line.rstrip() for line in log_path.read_text().splitlines() if line.strip()
    ]
    if not lines:
        return {
            "seed_infisical_last_success": "NEVER",
            "seed_infisical_last_run": "unknown (empty log)",
        }

    last_run = lines[-1]
    last_success = "NEVER"
    for line in reversed(lines):
        if "SUCCESS" in line:
            # Extract leading timestamp token if present
            parts = line.split()
            last_success = parts[0] if parts else line
            break

    return {
        "seed_infisical_last_success": last_success,
        "seed_infisical_last_run": last_run,
    }


def _gather_infra_health(env_sync_log_path: Path) -> dict[str, str]:
    """Gather all infra health values. Raises InfraHealthGatherError if any check fails."""
    health = _parse_env_sync_log(env_sync_log_path)

    health["infisical_container"] = _ssh(
        "docker ps --filter name=infisical --format '{{.Names}}.{{.Status}}'",
        "infisical",
    )
    health["deploy_agent_service"] = _ssh(
        "systemctl --user is-active deploy-agent.service",
        "deploy-agent",
    )
    health["runtime_effects_health"] = _ssh(
        "docker inspect omninode-runtime-effects --format='{{.State.Health.Status}}'",
        "runtime-effects",
    )
    health["kafka_redpanda"] = _ssh(
        "docker ps --filter name=redpanda --format '{{.Status}}'",
        "kafka-redpanda",
    )
    health["postgres"] = _ssh(
        "docker ps --filter name=postgres --format '{{.Status}}'",
        "postgres",
    )
    health["open_infra_blockers"] = os.environ.get("ONEX_INFRA_BLOCKER_TICKETS", "none")

    return health


class HandlerHandoffEffect:
    """Captures git session state and writes a YAML handoff artifact."""

    handler_type: Literal["node_handler"] = "node_handler"
    handler_category: Literal["effect"] = "effect"

    def handle(
        self,
        session_id: str,
        correlation_id: UUID,
        summary: str | None = None,
        cwd: str | None = None,
    ) -> dict[str, object]:
        """Capture state and write artifact.

        Args:
            session_id: Session identifier for artifact scoping.
            correlation_id: Correlation ID flowing through the pipeline.
            summary: Optional free-text note for next session.
            cwd: Working directory to capture from (defaults to process cwd).

        Returns:
            dict with artifact_path, captured_branches, captured_dirty_files.

        Raises:
            InfraHealthGatherError: If any mandatory infra health check cannot be sourced.
        """
        work_dir = cwd or os.getcwd()

        branch = _run(["git", "branch", "--show-current"], work_dir)
        commit = _run(["git", "log", "-1", "--format=%H"], work_dir)
        recent_commits_raw = _run(["git", "log", "--oneline", "-5"], work_dir)
        recent_commits = [line for line in recent_commits_raw.splitlines() if line]
        dirty_raw = _run(["git", "status", "--porcelain"], work_dir)
        dirty_files = [line[3:] for line in dirty_raw.splitlines() if line]

        cwd_hash = hashlib.sha256(work_dir.encode()).hexdigest()[:8]

        state_dir = Path(os.environ.get("ONEX_STATE_DIR", Path.home() / ".onex_state"))
        env_sync_log = state_dir / "logs" / "env-sync.log"

        # Raises InfraHealthGatherError on any SSH failure — fail loudly
        infra_health = _gather_infra_health(env_sync_log)

        artifact: dict[str, object] = {
            "version": 1,
            "created_at": datetime.now(UTC).isoformat(),
            "session_id": session_id,
            "correlation_id": str(correlation_id),
            "cwd": work_dir,
            "cwd_hash": cwd_hash,
            "summary": summary,
            "context": {
                "branch": branch or None,
                "commit": commit or None,
                "recent_commits": recent_commits,
                "dirty_files": dirty_files,
            },
            "infra_health": infra_health,
        }

        handoff_dir = state_dir / "session" / "handoff"
        handoff_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now(UTC).strftime("%Y-%m-%d-%H-%M-%S-%f")
        unique_suffix = uuid4().hex[:8]
        artifact_path = (
            handoff_dir / f"handoff-{timestamp}-{session_id[:8]}-{unique_suffix}.yaml"
        )

        fd, tmp_str = tempfile.mkstemp(dir=handoff_dir, suffix=".yaml.tmp")
        try:
            with os.fdopen(fd, "w") as f:
                yaml.dump(artifact, f, default_flow_style=False, allow_unicode=True)
            Path(tmp_str).rename(artifact_path)
        except Exception:
            Path(tmp_str).unlink(missing_ok=True)
            raise

        logger.info("Handoff artifact written: %s", artifact_path)

        return {
            "artifact_path": str(artifact_path),
            "captured_branches": [branch] if branch else [],
            "captured_dirty_files": dirty_files,
        }


__all__: list[str] = [
    "HandlerHandoffEffect",
    "InfraHealthGatherError",
    "_parse_env_sync_log",
]
