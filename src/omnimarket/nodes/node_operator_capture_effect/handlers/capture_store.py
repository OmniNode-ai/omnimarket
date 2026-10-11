# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local capture store of node_operator_capture_effect (OMN-20905).

The serve process takes each operator prompt from the bus into this store before anything else
happens, so a model or ledger outage loses nothing: the prompt waits in the inbox until its rows
are appended. Standard library only.

Layout under the store directory (``ONEX_OPERATOR_CAPTURE_DIR``, else
``~/.local/state/omni/operator-capture``):

* ``inbox/<stamp>-<pid>-<rand>.json``: one operator prompt as the bus delivered it, written
  atomically before anything else happens, so a crash later loses nothing;
* ``sessions/<session>.digests``: one digest per line of every prompt taken in for a session,
  so a redelivered record is taken in once;
* ``captures.jsonl``: one record per processed prompt (text, classification, rows written);
* ``pushed.json``: which overdue asks were pushed on which day;
* ``lock``: the fcntl lock one worker holds while it processes the inbox.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import tempfile
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STORE_ENV = "ONEX_OPERATOR_CAPTURE_DIR"
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def store_dir(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    configured = env.get(STORE_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".local" / "state" / "omni" / "operator-capture"


def text_digest(session_id: str, text: str) -> str:
    """Whitespace-insensitive digest of one message within one session."""
    normalized = " ".join(text.split())
    return hashlib.sha256(f"{session_id}\0{normalized}".encode()).hexdigest()[:24]


def _ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _atomic_write(path: Path, text: str) -> None:
    _ensure(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _append_line(path: Path, line: str) -> None:
    _ensure(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (line.rstrip("\n") + "\n").encode())
    finally:
        os.close(fd)


def session_digests(root: Path, session_id: str) -> frozenset[str]:
    path = root / "sessions" / f"{_SAFE.sub('_', session_id)}.digests"
    try:
        return frozenset(
            line.strip() for line in path.read_text().splitlines() if line.strip()
        )
    except OSError:
        return frozenset()


def ingest(
    root: Path,
    *,
    session_id: str,
    text: str,
    source: str,
    received_at: datetime,
    origin_event: str,
    transcript_path: str | None = None,
) -> Path | None:
    """Write one prompt to the inbox and record its digest; None when already taken in."""
    digest = text_digest(session_id, text)
    if digest in session_digests(root, session_id):
        return None
    stamp = received_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    record = {
        "session_id": session_id,
        "text": text,
        "source": source,
        "received_at": received_at.astimezone(UTC).isoformat(),
        "origin_event": origin_event,
        "transcript_path": transcript_path,
        "digest": digest,
    }
    path = root / "inbox" / f"{stamp}-{os.getpid()}-{secrets.token_hex(3)}.json"
    _atomic_write(path, json.dumps(record, ensure_ascii=False))
    _append_line(root / "sessions" / f"{_SAFE.sub('_', session_id)}.digests", digest)
    return path


def pending(root: Path) -> list[Path]:
    inbox = root / "inbox"
    if not inbox.is_dir():
        return []
    return sorted(p for p in inbox.glob("*.json") if not p.name.startswith("."))


def read_pending(path: Path) -> dict[str, Any]:
    loaded: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path.name} is not a JSON object")
    return loaded


def rewrite_pending(path: Path, record: Mapping[str, Any]) -> None:
    _atomic_write(path, json.dumps(dict(record), ensure_ascii=False, default=str))


def record_capture(root: Path, record: Mapping[str, Any]) -> None:
    _append_line(
        root / "captures.jsonl", json.dumps(record, ensure_ascii=False, default=str)
    )


def read_pushed(root: Path) -> dict[str, str]:
    try:
        loaded: object = json.loads((root / "pushed.json").read_text())
    except (OSError, ValueError):
        return {}
    return (
        {str(k): str(v) for k, v in loaded.items()} if isinstance(loaded, dict) else {}
    )


def write_pushed(root: Path, pushed: Mapping[str, str]) -> None:
    _atomic_write(root / "pushed.json", json.dumps(dict(pushed), sort_keys=True))


@contextlib.contextmanager
def worker_lock(root: Path) -> Iterator[bool]:
    """Hold the store's worker lock without waiting; yields False when another worker has it."""
    _ensure(root)
    fd = os.open(root / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


__all__ = [
    "STORE_ENV",
    "ingest",
    "pending",
    "read_pending",
    "read_pushed",
    "record_capture",
    "rewrite_pending",
    "session_digests",
    "store_dir",
    "text_digest",
    "worker_lock",
    "write_pushed",
]
