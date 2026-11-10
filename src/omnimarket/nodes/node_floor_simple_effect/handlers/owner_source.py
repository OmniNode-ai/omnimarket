# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuse owner entries from any source other than overlays main, naming the source (OMN-23388, R6 of the h053 cascade plan).

The floor's owner map is ``causes.owners`` in the floor overlay file. On 2026-11-09 the publisher loaded overlays main
while owner tickets added since 12:50Z sat only on a side branch, and every red row read nobody on it. The overlay's
``owner_source`` names the ref that is the one home of the map; the floor overlay file must be byte-equal to the same file
at that ref of its own git checkout. A checkout on a side branch with other entries, an uncommitted edit, a file outside
any checkout and a ref that cannot be read are each refused with the source named. Every git read is argv, with a timeout.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.floor_17a import ModelOwnerSourceOverlay

GitRunner = Callable[[Path, tuple[str, ...], int], tuple[int, str]]


def run_git(cwd: Path, args: tuple[str, ...], timeout_s: int) -> tuple[int, str]:
    """One git read in ``cwd``: the exit code and the stripped output (stderr when it failed)."""
    try:
        done = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
            env=scrub_git_location_env(os.environ),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, type(exc).__name__
    return done.returncode, (
        done.stdout if done.returncode == 0 else done.stderr
    ).strip()


def check_owner_source(
    path: Path, spec: ModelOwnerSourceOverlay, git: GitRunner = run_git
) -> None:
    """Raise ``NodeOverlayError`` naming the source unless ``path`` equals its checkout's ``spec.remote_ref`` copy."""
    cwd = path.parent
    code, top = git(cwd, ("rev-parse", "--show-toplevel"), spec.git_timeout_s)
    if code != 0 or not top:
        raise NodeOverlayError(
            f"owner entries refused from {path}: it is not in a git checkout, so it cannot be shown to be {spec.remote_ref}"
        )
    rel = path.resolve().relative_to(Path(top).resolve()).as_posix()
    code, wanted = git(
        cwd, ("rev-parse", f"{spec.remote_ref}:{rel}"), spec.git_timeout_s
    )
    if code != 0:
        raise NodeOverlayError(
            f"owner entries refused from {path}: {spec.remote_ref} cannot be read there ({wanted[:120]})"
        )
    code, held = git(cwd, ("hash-object", "--", path.name), spec.git_timeout_s)
    if code != 0 or held != wanted:
        _, branch = git(cwd, ("rev-parse", "--abbrev-ref", "HEAD"), spec.git_timeout_s)
        _, head = git(cwd, ("rev-parse", "--short=12", "HEAD"), spec.git_timeout_s)
        raise NodeOverlayError(
            f"owner entries refused from {path} (checkout on {branch or 'an unreadable branch'} at {head or 'an unreadable head'}): "
            f"the floor overlay differs from {spec.remote_ref}, the only accepted source"
        )
