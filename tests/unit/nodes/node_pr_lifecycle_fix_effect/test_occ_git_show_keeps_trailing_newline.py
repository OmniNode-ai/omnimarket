# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20040: a contract read from a git ref keeps its final newline.

The batch-window writer reads a member contract with ``git show`` and writes it
as a new contract file. ``run_git`` must preserve stdout for that contract so
the file retains its final newline.
"""

from __future__ import annotations

import inspect
import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_pr_lifecycle_fix_effect.handlers import (
    occ_companion_emitter,
)
from omnimarket.occ_git_transport import run_git

CONTRACT = 'schema_version: "1.0.0"\ndod_evidence:\n  - id: "a"\n'


def _repo(tmp_path: Path) -> str:
    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            env=scrub_git_location_env(os.environ),
        )

    git("init", "-q")
    (tmp_path / "c.yaml").write_text(CONTRACT, encoding="utf-8")
    git("add", "c.yaml")
    git("commit", "-q", "-m", "c")
    return str(tmp_path)


@pytest.mark.unit
def test_run_git_strip_false_returns_the_bytes(tmp_path: Path) -> None:
    cwd = _repo(tmp_path)
    argv = ["git", "show", "HEAD:c.yaml"]
    assert run_git(argv, cwd=cwd, strip=False) == CONTRACT
    assert run_git(argv, cwd=cwd) == CONTRACT.rstrip("\n")


@pytest.mark.unit
def test_batch_contract_read_does_not_strip() -> None:
    src = inspect.getsource(occ_companion_emitter.OccCompanionEmitter)
    anchor = 'f"FETCH_HEAD:{branch_contract_path}"'
    window = src[src.index(anchor) : src.index(anchor) + 400]
    assert "strip=False" in window
