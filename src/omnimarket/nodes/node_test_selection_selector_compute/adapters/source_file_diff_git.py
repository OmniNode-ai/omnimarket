# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real backend: the changed keys of a declared data file between two git refs (OMN-26275)."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence

from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_file_diff_in_memory import (
    diff_toml_texts,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_path_resolution import (
    ModelDataFileChange,
)

_GIT_TIMEOUT_SECONDS = 30
# A git hook exports these, and they override `git -C`: a read run from a hook would read the invoking
# worktree, not ``repo_root``.
_GIT_LOCATION_ENV = frozenset(
    {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"}
)


def _show(repo_root: str, ref: str, path: str) -> str | None:
    """The text of ``path`` at ``ref``; None when git cannot give it (unknown ref, absent file)."""
    try:
        done = subprocess.run(
            ["git", "-C", repo_root, "show", f"{ref}:{path}"],
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
            env={k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_ENV},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", errors="replace")


class SourceFileDiffGit:
    """Reads both sides with ``git show`` and compares them with the shared TOML comparison."""

    def changes(
        self, repo_root: str, base_ref: str, head_ref: str, paths: Sequence[str]
    ) -> tuple[ModelDataFileChange, ...]:
        return tuple(
            diff_toml_texts(
                path, _show(repo_root, base_ref, path), _show(repo_root, head_ref, path)
            )
            for path in paths
        )
