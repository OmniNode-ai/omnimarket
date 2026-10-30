# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Checkpoint Workflow launches and TaskStop results from a PostToolUse payload, bound to a principal.

A Workflow launch is recorded (task id, run id, script, purpose, role) in this session's checkpoint;
a TaskStop marks its workflow stopped. The session's principal is bound before any existing record
is read or validated, and every record another principal wrote (or that names no principal, in a
member session) is dropped first. Of what is left:

* the base is this session's newest checkpoint, else the newest checkpoint of the same member (any
  checkpoint, in an operator session), and only when it is under ``MAX_AGE_HOURS`` old;
* decisions, deadlines, ticks and notes are inherited from that base only; workflows only from this
  session's own checkpoint;
* a TaskStop marks a workflow only when this session's own checkpoint names it.

The session lane comes from the newest ``session.env`` under the sessions directory. A member
session considers only a ``session.env`` whose ``SESSION_PRINCIPAL=`` is one of its own principals;
one with no principal, or another principal's, is unverified and skipped, and the lane falls back
to ``session-<session id>``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omnimarket.session_scope import ModelSessionScope

from . import checkpoint as ckpt


class SkippedError(Exception):
    """The payload records nothing; the message says why."""


def response_is_error(response: Any) -> bool:
    if isinstance(response, str):
        return bool(re.search(r"(?im)^\s*(?:error|exception|failed)\b", response))
    if isinstance(response, dict):
        return bool(
            response.get("error")
            or response.get("isError")
            or response.get("status") in ("error", "failed")
        )
    return False


def response_record(response: Any) -> dict[str, Any] | None:
    if isinstance(response, str):
        task = re.search(r"(?m)^\s*Task ID:\s*(\S+)", response)
        run = re.search(r"(?m)^\s*Run ID:\s*(\S+)", response)
        return {"taskId": task[1], "runId": run[1] if run else ""} if task else None
    if isinstance(response, dict):
        return response if response.get("taskId") else None
    return None


def project_slug(path: str) -> str:
    """The harness names a project directory after its start path, non-alphanumerics as '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(path).resolve()))


def applies(
    payload: dict[str, Any],
    *,
    entrypoint: str | None,
    hook_mode: str | None,
    project_home: str | None,
) -> bool:
    if hook_mode == "force":
        return True
    if entrypoint != "cli":
        return False
    transcript = str(payload.get("transcript_path") or "")
    if not project_home or not transcript:
        return True  # cannot tell; record rather than drop
    return Path(transcript).parent.name == project_slug(project_home)


def _env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            values.setdefault(key.strip(), value.strip())
    return values


def session_lane(
    session_id: str,
    now: datetime,
    scope: ModelSessionScope,
    sessions_dir: Path | None,
) -> str:
    """The lane of the newest recent ``session.env`` this session may claim, else ``session-<id>``."""
    if sessions_dir is not None:
        candidates: list[tuple[float, str]] = []
        for path in sessions_dir.glob("*/session.env"):
            try:
                modified = path.stat().st_mtime
                if not 0 <= now.timestamp() - modified <= ckpt.MAX_AGE_HOURS * 3600:
                    continue
                values = _env_values(path)
            except OSError:
                continue
            principal = values.get("SESSION_PRINCIPAL") or None
            if not scope.owns(principal):
                continue
            lane = values.get("SESSION_LANE", "")
            if ckpt.LANE_RE.fullmatch(lane):
                candidates.append((modified, lane))
        if candidates:
            return max(candidates)[1]
    return f"session-{session_id[:8]}"


def record(
    payload: dict[str, Any],
    *,
    directory: Path,
    scope: ModelSessionScope,
    now: datetime,
    sessions_dir: Path | None,
) -> tuple[Path, str, dict[str, Any]]:
    """Write the checkpoint the payload calls for; raise :class:`SkippedError` when it calls for none."""
    tool = payload.get("tool_name")
    if tool not in ("Workflow", "TaskStop"):
        raise SkippedError(f"tool {tool!r} is not checkpointed")
    if response_is_error(payload.get("tool_response")):
        raise SkippedError("the tool returned an error")
    response = (
        response_record(payload.get("tool_response")) if tool == "Workflow" else None
    )
    if tool == "Workflow" and response is None:
        raise SkippedError("the Workflow response carries no task id")
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise ckpt.RefusedError(["session_id must be a nonempty string"])
    # Bound first: every record below is one this session's principal owns.
    all_records = ckpt.records(directory, scope)
    same_session = [d for d in all_records if d.get("session_id") == session_id]
    inputs = payload.get("tool_input") or {}
    task_id = (
        inputs.get("task_id") if tool == "TaskStop" else (response or {})["taskId"]
    )
    if tool == "TaskStop" and not any(
        wf["task_id"] == task_id for d in same_session for wf in d["workflows"]
    ):
        raise SkippedError(f"task {task_id} is not in this session's own checkpoint")
    base = (
        same_session[0] if same_session else (all_records[0] if all_records else None)
    )
    if base and ckpt.age_hours(base, now) > ckpt.MAX_AGE_HOURS:
        base = None
    own = base is not None and base.get("session_id") == session_id
    data: dict[str, Any] = {
        "schema": ckpt.SCHEMA_ID,
        "written_at": ckpt.stamp(now),
        "session_lane": base["session_lane"]
        if own and base
        else session_lane(session_id, now, scope, sessions_dir),
        "session_id": session_id,
        "workflows": [dict(wf) for wf in base["workflows"]] if own and base else [],
    }
    if scope.writer_principal is not None:
        data["principal"] = scope.writer_principal
    for field in ("decisions", "deadlines", "ticks", "notes"):
        data[field] = list(base.get(field) or []) if base else []
    if tool == "TaskStop":
        matched = False
        for old in data["workflows"]:
            if old["task_id"] == task_id:
                old["status"] = "stopped"
                matched = True
        if not matched:
            raise SkippedError(
                f"task {task_id} is not in this session's current checkpoint"
            )
        action = f"marked task={task_id} stopped"
    else:
        assert response is not None
        script = response.get("scriptPath") or inputs.get("scriptPath") or "inline"
        name = response.get("workflowName") or ""
        drain = any(
            re.search(r"repo[-_]drains|merge-drain", value)
            for value in (name, Path(script).name)
        )
        wf: dict[str, Any] = {
            "task_id": task_id,
            "run_id": response.get("runId", ""),
            "script": script,
            "purpose": (response.get("summary") or name or Path(script).name)[:300],
            "status": "running",
            "role": "drain" if drain else "lane",
        }
        if drain:
            wf["ends_at"] = ckpt.stamp(now + timedelta(hours=4))
        data["workflows"] = [
            old
            for old in data["workflows"]
            if old["task_id"] != task_id
            and not (wf["run_id"] and old["run_id"] == wf["run_id"])
        ]
        data["workflows"].append(wf)
        action = f"recorded task={task_id} run={wf['run_id']}"
    findings = ckpt.validate(data)
    if findings:
        raise ckpt.RefusedError(findings)
    return ckpt.write(data, directory), action, data


def utc_now() -> datetime:
    return datetime.now(UTC)
