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


def _run(args: list[str], cwd: str) -> str:
    try:
        result = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, OSError, subprocess.SubprocessError) as exc:
        logger.warning("subprocess call failed %s: %s", args, exc)
        return ""


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
        """
        work_dir = cwd or os.getcwd()

        branch = _run(["git", "branch", "--show-current"], work_dir)
        commit = _run(["git", "log", "-1", "--format=%H"], work_dir)
        recent_commits_raw = _run(["git", "log", "--oneline", "-5"], work_dir)
        recent_commits = [line for line in recent_commits_raw.splitlines() if line]
        dirty_raw = _run(["git", "status", "--porcelain"], work_dir)
        dirty_files = [line[3:] for line in dirty_raw.splitlines() if line]

        cwd_hash = hashlib.sha256(work_dir.encode()).hexdigest()[:8]

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
        }

        state_dir = Path(os.environ.get("ONEX_STATE_DIR", Path.home() / ".onex_state"))
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


__all__: list[str] = ["HandlerHandoffEffect"]
