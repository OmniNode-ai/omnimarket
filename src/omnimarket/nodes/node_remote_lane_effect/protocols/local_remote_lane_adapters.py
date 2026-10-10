# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local adapters of the remote-lane effect ports (OMN-20669)."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

from .protocol_remote_lane_effect import RemoteLaneCommandOutcome, RemoteLanePortError

# A git hook exports these into its environment, and they override ``git -C <clone>``:
# a read run under a hook would read the invoking repository, not the clone. Mirrors
# omnibase_core.validators.no_unguarded_git_subprocess.scrub_git_location_env without
# importing that test-scanning module at runtime.
_GIT_LOCATION_ENV_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CEILING_DIRECTORIES",
)


def _scrubbed_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG")}
    for key in _GIT_LOCATION_ENV_VARS:
        env.pop(key, None)
    return env


class LocalRemoteLaneGit:
    """Runs ``git`` on this host, with no shell and no inherited git location."""

    def is_clone(self, path: str) -> bool:
        return (Path(path) / ".git").exists()

    def run(self, argv: Sequence[str], timeout_s: float) -> RemoteLaneCommandOutcome:
        try:
            done = subprocess.run(
                list(argv),
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_s,
                env=_scrubbed_env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RemoteLanePortError(f"{type(exc).__name__}: {exc}") from exc
        return RemoteLaneCommandOutcome(done.returncode, done.stdout, done.stderr)
