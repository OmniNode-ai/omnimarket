# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared accessors for Onex session-state projection artifacts."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_TIMESTAMP_FIELDS = ("timestamp", "created_at", "createdAt", "emitted_at", "saved_at")
_AGENT_FIELDS = ("agent_name", "agent_id", "agent", "worker_name", "worker_id")
_ACTION_FIELDS = ("action", "event_type", "type", "message", "summary")


def resolve_onex_state_dir(state_dir: Path | str | None = None) -> Path:
    """Resolve the Onex state directory without hard-coded user paths."""
    if state_dir is not None:
        return Path(state_dir).expanduser()

    configured = os.environ.get("ONEX_STATE_DIR", "")
    if configured:
        return Path(configured).expanduser()

    omni_home = os.environ.get("OMNI_HOME", "")
    if omni_home:
        return Path(omni_home).expanduser() / ".onex_state"

    cwd = Path.cwd()
    for candidate in (cwd, *cwd.parents):
        state_candidate = candidate / ".onex_state"
        if state_candidate.exists():
            return state_candidate

    return cwd / ".onex_state"


def validate_projection_id(value: str, *, field_name: str) -> str:
    """Validate IDs used as projection filenames."""
    if not value or not _SAFE_ID_RE.fullmatch(value) or ".." in value:
        raise ValueError(
            f"{field_name} must contain only letters, numbers, '.', '_', '-', or ':'"
        )
    return value


def parse_event_timestamp(value: object) -> datetime | None:
    """Parse a JSON event timestamp into an aware UTC datetime."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def extract_event_timestamp(event: dict[str, Any]) -> datetime | None:
    """Return the first timestamp value declared by a projection event."""
    for field_name in _TIMESTAMP_FIELDS:
        parsed = parse_event_timestamp(event.get(field_name))
        if parsed is not None:
            return parsed
    return None


def extract_agent_identity(event: dict[str, Any]) -> str:
    """Return the first agent identity field on an event."""
    for field_name in _AGENT_FIELDS:
        value = event.get(field_name)
        if isinstance(value, str) and value:
            return value
    return ""


def summarize_event_action(event: dict[str, Any]) -> str:
    """Build a stable action summary from an event payload."""
    for field_name in _ACTION_FIELDS:
        value = event.get(field_name)
        if isinstance(value, str) and value:
            return value
    return "event"


@dataclass(frozen=True)
class SessionProjectionSnapshot:
    """Materialized session state plus ordering metadata."""

    state: dict[str, Any]
    source_path: Path
    observed_at: datetime | None


class CheckpointProjectionStore:
    """Filesystem-backed Onex checkpoint projection store."""

    def __init__(self, state_dir: Path | str | None = None) -> None:
        self.state_dir = resolve_onex_state_dir(state_dir)
        self.checkpoint_dir = self.state_dir / "checkpoints"

    def save(self, checkpoint_id: str, payload: dict[str, Any]) -> None:
        """Persist a checkpoint payload at its deterministic projection key."""
        safe_id = validate_projection_id(checkpoint_id, field_name="checkpoint_id")
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        path = self.checkpoint_dir / f"{safe_id}.json"
        tmp_path = path.with_suffix(".json.tmp")
        content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(path)

    def load(self, checkpoint_id: str) -> dict[str, Any] | None:
        """Load a checkpoint payload by deterministic projection key."""
        safe_id = validate_projection_id(checkpoint_id, field_name="checkpoint_id")
        path = self.checkpoint_dir / f"{safe_id}.json"
        if not path.is_file():
            return None
        return _read_json_mapping(path)

    def list_ids(self) -> list[str]:
        """Return all checkpoint IDs in stable order."""
        if not self.checkpoint_dir.exists():
            return []
        return sorted(
            path.stem
            for path in self.checkpoint_dir.glob("*.json")
            if path.is_file() and path.parent == self.checkpoint_dir
        )

    def find_session_snapshots(
        self, *, task_id: str, agent_id: str
    ) -> list[SessionProjectionSnapshot]:
        """Find checkpoint projections matching the requested task and agent."""
        snapshots: list[SessionProjectionSnapshot] = []
        snapshots.extend(
            self._iter_checkpoint_snapshots(task_id=task_id, agent_id=agent_id)
        )
        snapshots.extend(
            self._iter_pipeline_checkpoint_snapshots(task_id=task_id, agent_id=agent_id)
        )
        return sorted(
            snapshots,
            key=lambda item: (
                item.observed_at or _file_mtime_utc(item.source_path),
                str(item.source_path),
            ),
            reverse=True,
        )

    def _iter_checkpoint_snapshots(
        self, *, task_id: str, agent_id: str
    ) -> Iterable[SessionProjectionSnapshot]:
        if not self.checkpoint_dir.exists():
            return
        expected_ids = {
            task_id,
            f"{task_id}-{agent_id}",
            f"{task_id}:{agent_id}",
            f"{task_id}_{agent_id}",
        }
        for path in sorted(self.checkpoint_dir.glob("*.json")):
            if not path.is_file():
                continue
            raw = _read_json_mapping(path)
            if raw is None:
                continue
            if path.stem in expected_ids or _matches_task_agent(
                raw, task_id=task_id, agent_id=agent_id
            ):
                yield SessionProjectionSnapshot(
                    state=raw,
                    source_path=path,
                    observed_at=extract_event_timestamp(raw),
                )

    def _iter_pipeline_checkpoint_snapshots(
        self, *, task_id: str, agent_id: str
    ) -> Iterable[SessionProjectionSnapshot]:
        root = self.state_dir / "pipeline_checkpoints" / task_id
        if not root.exists():
            return
        for path in sorted(root.glob("*.json")):
            if not path.is_file():
                continue
            raw = _read_json_mapping(path)
            if raw is None:
                continue
            if _matches_task_agent(raw, task_id=task_id, agent_id=agent_id):
                yield SessionProjectionSnapshot(
                    state=raw,
                    source_path=path,
                    observed_at=extract_event_timestamp(raw),
                )


class EventProjectionStore:
    """Read-only store for Onex JSONL event projections."""

    def __init__(self, state_dir: Path | str | None = None) -> None:
        self.state_dir = resolve_onex_state_dir(state_dir)

    def matching_events(
        self, *, agent_name: str, anchor: datetime, window_seconds: int
    ) -> list[dict[str, Any]]:
        """Return events for an agent in the inclusive rewind window."""
        if window_seconds < 0:
            raise ValueError("window_seconds must be non-negative")

        start = anchor.timestamp() - window_seconds
        matched: list[tuple[datetime, str, int, dict[str, Any]]] = []
        for path in self._event_log_paths():
            for line_number, event in _iter_jsonl_events(path):
                event_time = extract_event_timestamp(event)
                if event_time is None:
                    continue
                event_seconds = event_time.timestamp()
                if event_seconds < start or event_seconds > anchor.timestamp():
                    continue
                if extract_agent_identity(event) != agent_name:
                    continue
                matched.append((event_time, str(path), line_number, event))
        return [event for _, _, _, event in sorted(matched)]

    def _event_log_paths(self) -> list[Path]:
        candidates: list[Path] = []
        for relative in (
            "events",
            "event-log",
            "dispatch-log",
            "delegation-events",
            "llm-cost-events",
        ):
            root = self.state_dir / relative
            if root.exists():
                candidates.extend(sorted(root.glob("*.jsonl")))
                candidates.extend(sorted(root.glob("*.ndjson")))

        for filename in ("dispatch-log.ndjson", "events.jsonl", "event-log.jsonl"):
            path = self.state_dir / filename
            if path.is_file():
                candidates.append(path)

        return sorted({path.resolve(): path for path in candidates}.values())


def load_session_phase_projection(
    state_dir: Path | str | None = None,
) -> dict[str, Any] | None:
    """Load the canonical session phase projection when present."""
    path = resolve_onex_state_dir(state_dir) / "session" / "phase_state.yaml"
    if not path.is_file():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else None


def _matches_task_agent(
    payload: dict[str, Any], *, task_id: str, agent_id: str
) -> bool:
    task_values = {payload.get("task_id"), payload.get("ticket_id"), payload.get("id")}
    agent_values = {
        payload.get("agent_id"),
        payload.get("agent_name"),
        payload.get("worker_id"),
        payload.get("worker_name"),
    }
    nested = payload.get("session_state")
    if isinstance(nested, dict):
        task_values.update({nested.get("task_id"), nested.get("ticket_id")})
        agent_values.update({nested.get("agent_id"), nested.get("agent_name")})
    return task_id in task_values and agent_id in agent_values


def _read_json_mapping(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _iter_jsonl_events(path: Path) -> Iterable[tuple[int, dict[str, Any]]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict):
            yield line_number, raw


def _file_mtime_utc(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
