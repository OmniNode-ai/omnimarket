# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared seeding for the OCC observation write-EFFECT tests.

Every mutate-path test in this package clones a stand-in for
``onex_change_control``. Since OMN-15323 that stand-in must carry the real
contract + receipt tree, because the handler now authors self-bind evidence
into it and fails LOUD when the cited contract is absent — the same way the
real clone would if the evidence ticket were mis-pointed. Seeding an empty repo
would test a repository shape that cannot exist in production.

The fixture content is copied verbatim from ``onex_change_control@dev``; see
``tests/fixtures/occ_observation_selfbind/README.md``.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

OCC_FIXTURE_ROOT = (
    Path(__file__).resolve().parents[3] / "fixtures" / "occ_observation_selfbind"
)


def git(cwd: Path, *args: str) -> str:
    """Run git in ``cwd`` and return stripped stdout."""
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def clear_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clear inherited GIT_* vars so git ops target the temp repo.

    ``GIT_DIR``/``GIT_WORK_TREE`` override both ``-C`` and the cwd, so a test
    run from inside a worktree would otherwise operate on the real repository
    (reference_git_env_vars_override_c_and_cwd).
    """
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def seed_occ_repo(tmp_path: Path) -> Callable[..., Path]:
    """Return a factory building a real local git repo shaped like OCC ``dev``."""

    def _seed(name: str = "seed") -> Path:
        seed = tmp_path / name
        shutil.copytree(OCC_FIXTURE_ROOT, seed)
        git(seed, "init", "-q")
        git(seed, "config", "user.name", "test")
        git(seed, "config", "user.email", "test@omninode.ai")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", "OCC fixture base")
        return seed

    return _seed
