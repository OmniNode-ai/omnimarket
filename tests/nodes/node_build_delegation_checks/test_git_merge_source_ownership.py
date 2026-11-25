# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The merge source reads a clone the runtime container does not own (the dev runtime mounts /app/omnimarket read-only, owned by
the host user), where git refuses the repository as dubious ownership unless the clone is named as safe for that one command."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omnimarket.nodes.node_build_delegation_checks.adapters import git_merge_source
from omnimarket.nodes.node_build_delegation_checks.adapters.git_merge_source import (
    GitMergeSource,
)


def test_the_git_log_names_only_the_clone_it_reads_as_a_safe_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clone = tmp_path / "omnimarket"
    seen: list[list[str]] = []

    def fake_run(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(git_merge_source.subprocess, "run", fake_run)

    assert GitMergeSource(clone).merges_between("a" * 40, "b" * 40) == ()

    (argv,) = seen
    assert argv[:3] == ["git", "-c", f"safe.directory={clone}"]
    assert "*" not in " ".join(argv)
    assert argv[argv.index("-C") + 1] == str(clone)
