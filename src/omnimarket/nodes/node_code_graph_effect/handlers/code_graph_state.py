# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the code graph builds leave in the state directory: the built marker, the last refresh
attempt, and the one lock the writing builds take.

The refresh on merge and the nightly build share the state directory and the staged files in it,
so one ``fcntl`` lock there lets exactly one writing build run at a time. The built marker is the
refresh's record of the last good build: it is written only when the ledger holds the build's row
(``written`` or ``already-written``), so a probe, a failed run or a drill never makes the next
refresh answer ``fresh``. The attempt record is the refresh's own: a build that fails leaves the
marker old, so the attempt is what keeps the next ticks from rerunning the full build.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.models.model_code_graph import EnumGraphFault, ModelCodeGraphResult

#: What a real, written build leaves; the refresh reads it instead of rebuilding.
BUILT_MARKER = "code-graph-built.json"
#: When the refresh last started a build, whether or not it completed.
ATTEMPT_MARKER = "code-graph-refresh-attempt.json"
#: The lock the writing builds of this state directory take.
BUILD_LOCK = "code-graph-build.lock"
#: The ledger results that mean the build's row is in the ledger.
LEDGER_HELD = ("written", "already-written")


def stage(target: Path, text: str) -> None:
    """Write ``text`` to ``target`` through a sibling temporary file of this process, then replace it."""
    staged = target.with_suffix(f".{os.getpid()}.tmp")
    staged.write_text(text, encoding="utf-8")
    staged.replace(target)


def keeps_marker(result: ModelCodeGraphResult) -> bool:
    """True only for a completed, clean, non-drill build whose row the ledger holds.

    A probe reports ``ledger=not-written`` and appends nothing, a failed append reports
    ``append-failed:<why>``: neither may make the next refresh answer ``fresh``.
    """
    return (
        result.status == "completed"
        and result.fault is EnumGraphFault.NONE
        and bool(result.repos)
        and result.ledger in LEDGER_HELD
    )


def write_marker(directory: Path, result: ModelCodeGraphResult, now: datetime) -> bool:
    """Write the built marker for ``result`` when it keeps one; whether it did."""
    if not keeps_marker(result):
        return False
    directory.mkdir(parents=True, exist_ok=True)
    stage(
        directory / BUILT_MARKER,
        json.dumps(
            {
                "built_at": now.astimezone(UTC).isoformat(),
                "heads": {repo.repo: repo.head[:12] for repo in result.repos},
            },
            indent=1,
        ),
    )
    return True


def read_marker(directory: Path) -> tuple[datetime | None, dict[str, str]]:
    """The last build's time and heads from the marker; none when it is absent or unreadable."""
    try:
        document = json.loads((directory / BUILT_MARKER).read_text(encoding="utf-8"))
        built_at = datetime.fromisoformat(str(document["built_at"]))
        heads = {str(name): str(value) for name, value in document["heads"].items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None, {}
    return built_at.astimezone(UTC), heads


def record_attempt(directory: Path, now: datetime) -> None:
    """Record that the refresh started a build at ``now``."""
    directory.mkdir(parents=True, exist_ok=True)
    stage(
        directory / ATTEMPT_MARKER,
        json.dumps({"attempted_at": now.astimezone(UTC).isoformat()}),
    )


def read_attempt(directory: Path) -> datetime | None:
    """When the refresh last started a build; none when it never did or the record is unreadable."""
    try:
        document = json.loads((directory / ATTEMPT_MARKER).read_text(encoding="utf-8"))
        return datetime.fromisoformat(str(document["attempted_at"])).astimezone(UTC)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


@contextmanager
def build_lock(
    directory: Path | None, *, wait: timedelta = timedelta(0)
) -> Iterator[bool]:
    """Hold the state directory's one build lock; yields whether this run holds it.

    Waits up to ``wait`` for a holder to finish, then yields ``False`` and the caller defers. A run
    with no state directory has nothing shared to protect and holds it trivially. The lock is an
    ``fcntl`` lock on an open file, so it is released when the process ends however it ends.
    """
    if directory is None:
        yield True
        return
    directory.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait.total_seconds()
    with (directory / BUILD_LOCK).open("a") as handle:
        held = False
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.5)
        try:
            yield held
        finally:
            if held:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
