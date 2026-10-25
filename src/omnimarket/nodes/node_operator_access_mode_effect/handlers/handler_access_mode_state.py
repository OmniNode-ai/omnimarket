# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The access-mode state files (OMN-20892).

One directory, default ``~/.claude/state/access-mode`` (``OMNI_ACCESS_MODE_STATE_DIR`` moves it):

* ``override`` holds one word, ``local`` or ``remote``, set by ``onex-access-mode set``; absent
  means detection decides. It applies to every operator-facing session, never to a headless one.
* ``silent`` holds ``on`` while global silent mode is enabled; absent means off.
* ``session-<id>.json`` holds one session's detected mode, its prompt-command override and the
  time of the operator's last prompt, which the push gate reads for the 30-minute idle rule.

Every write replaces the file atomically. Every read tolerates a missing or unreadable file.
Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

STATE_DIR_ENV = "OMNI_ACCESS_MODE_STATE_DIR"
OVERRIDE_ENV = "OMNI_ACCESS_MODE"
OVERRIDE_FILE = "override"
SILENT_FILE = "silent"
SETTABLE_MODES = ("local", "remote")
STALE_SESSION_SECONDS = 14 * 24 * 3600


def state_dir(environ: Mapping[str, str]) -> Path:
    configured = environ.get(STATE_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".claude" / "state" / "access-mode"


def session_file(directory: Path, session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id.strip()) or "unknown"
    return directory / f"session-{safe}.json"


def _replace(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def read_session(directory: Path, session_id: str) -> dict[str, Any]:
    if not session_id:
        return {}
    try:
        loaded: object = json.loads(
            session_file(directory, session_id).read_text("utf-8")
        )
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def update_session(directory: Path, session_id: str, **fields: Any) -> dict[str, Any]:
    """Merge ``fields`` into the session's state; a None value removes that key."""
    if not session_id:
        return {}
    state = read_session(directory, session_id)
    for key, value in fields.items():
        if value is None:
            state.pop(key, None)
        else:
            state[key] = value
    state["session_id"] = session_id
    state["updated_at"] = time.time()
    _replace(
        session_file(directory, session_id), json.dumps(state, sort_keys=True) + "\n"
    )
    return state


def read_global_override(directory: Path) -> str:
    try:
        word = (directory / OVERRIDE_FILE).read_text("utf-8").strip().lower()
    except OSError:
        return ""
    return word if word in SETTABLE_MODES else ""


def write_global_override(directory: Path, word: str | None) -> None:
    """Set the global override to ``local`` or ``remote``; None clears it (detection decides)."""
    target = directory / OVERRIDE_FILE
    if word is None:
        target.unlink(missing_ok=True)
        return
    if word not in SETTABLE_MODES:
        raise ValueError(f"access mode must be one of {SETTABLE_MODES}, not {word!r}")
    _replace(target, word + "\n")


def read_global_silent(directory: Path) -> bool:
    try:
        return (directory / SILENT_FILE).read_text("utf-8").strip().lower() == "on"
    except OSError:
        return False


def write_global_silent(directory: Path, enabled: bool) -> None:
    target = directory / SILENT_FILE
    if enabled:
        _replace(target, "on\n")
    else:
        target.unlink(missing_ok=True)


def prune_stale_sessions(directory: Path, now: float | None = None) -> int:
    """Remove session files untouched for 14 days; returns how many went."""
    cutoff = (time.time() if now is None else now) - STALE_SESSION_SECONDS
    removed = 0
    try:
        candidates = list(directory.glob("session-*.json"))
    except OSError:
        return 0
    for path in candidates:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed
