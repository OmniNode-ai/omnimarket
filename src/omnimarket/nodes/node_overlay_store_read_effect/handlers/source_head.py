# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where overlays main stands on this host, read from the clone the host's overlay root sits in (OMN-26432).

A snapshot's stamp names the revision it was loaded from (``source_ref`` is ``<overlay>@<sha>``). The host's overlay
root is a directory of the overlays clone, and the canonical clone sync keeps that clone's ``origin/main`` at the head of
the branch, so the sha the clone names is the revision the store should hold next. A host with no such clone answers
``None`` and the reader then behaves as it did before this read existed.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

ROOTS_ENV = "ONEX_SKILL_OVERLAY_ROOTS"
SOURCE_BRANCH = "main"
_GIT_TIMEOUT_S = 15


def _git(path: str, *argv: str) -> str | None:
    try:
        found = subprocess.run(
            ["git", "-C", path, *argv],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_S,
            env=scrub_git_location_env(os.environ),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return found.stdout.strip() if found.returncode == 0 and found.stdout else None


def overlays_main_sha(env: Mapping[str, str]) -> str | None:
    """The sha ``origin/main`` names in the clone of the first overlay root that sits in one; ``None`` when none does.

    The first root is the overlays checkout in every unit the installer renders; a later root is a plugin's own overlays."""
    for root in env.get(ROOTS_ENV, "").split(os.pathsep):
        if not root.strip():
            continue
        top = _git(root, "rev-parse", "--show-toplevel")
        if top is None:
            continue
        return _git(top, "rev-parse", f"refs/remotes/origin/{SOURCE_BRANCH}")
    return None


def revision_of(source_ref: str) -> str:
    """The sha of a ``<overlay>@<sha>`` source ref; the whole ref when it names none."""
    return source_ref.rpartition("@")[2]


__all__ = ["ROOTS_ENV", "SOURCE_BRANCH", "overlays_main_sha", "revision_of"]
