# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Clone-refusal lock line judging for the host reconcile effect.

The verifier script prints a stale-lock line for every ``*.lock`` file in a
refused clone's git dir older than the converge timeout, judged only by age.
The canonical clone sync keeps flock files on purpose and holds them with
``fcntl.flock`` while a sync runs, so a DECLARED persistent flock file is
re-judged here by its live lock state: free means nothing holds it and the
line is dropped; held means a live process owns it and the file must not be
removed. This module is POSIX only (``fcntl``).
"""

from __future__ import annotations

import fcntl
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import yaml

from omnimarket.handlers.node_overlay_reader import find_node_overlay
from omnimarket.models.model_clone_lock_policy import (
    ModelCloneLockOverlay,
    ModelCloneLockPolicy,
)

LOCK_LINE_PATTERN = re.compile(r"^stale ref lock: (?P<path>.+?) age \d+s")

LockState = Literal["held", "free", "unreadable"]


def load_clone_lock_policy() -> ModelCloneLockPolicy:
    """The contract's declared persistent flock names plus the overlay's additions."""
    contract = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "contract.yaml").read_text()
    )
    policy = ModelCloneLockPolicy.model_validate(contract["clone_locks"])
    path = find_node_overlay("node_host_reconcile_effect")
    if path is None:
        return policy
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("host reconcile overlay must be a mapping")
    overlay = ModelCloneLockOverlay.model_validate(document.get("clone_locks", {}))
    return policy.model_copy(
        update={
            "persistent_flock_files": tuple(
                dict.fromkeys(
                    (
                        *policy.persistent_flock_files,
                        *overlay.additional_persistent_flock_files,
                    )
                )
            )
        }
    )


def flock_state(path: Path) -> LockState:
    """Judge a lock file by whether a flock can be taken on it, not by its age.

    A non-blocking shared flock on a read-only descriptor succeeds only when no
    live process holds the file locked; it is dropped at once by closing the
    descriptor. ``held`` is a ``BlockingIOError``; a file that cannot be opened,
    or a probe failing with any other ``OSError``, is ``unreadable``.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return "unreadable"
    try:
        fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return "held"
    except OSError:
        return "unreadable"
    finally:
        os.close(descriptor)
    return "free"


def describe_lock_line(
    text: str,
    policy: ModelCloneLockPolicy,
    state_of: Callable[[Path], LockState] = flock_state,
) -> str | None:
    """The text to print for one verifier ``lock`` line, or None to print nothing.

    A line whose path cannot be parsed, and a line naming a file that is not a
    declared persistent flock file, are returned unchanged. A declared file is
    judged by its lock state.
    """
    match = LOCK_LINE_PATTERN.match(text)
    if match is None:
        return text
    path = Path(match.group("path"))
    if path.name not in policy.persistent_flock_files:
        return text
    state = state_of(path)
    if state == "free":
        return None
    if state == "held":
        return f"lock held by a live process, not stale: {match.group('path')}"
    return f"lock unreadable, not judged: {match.group('path')}"
