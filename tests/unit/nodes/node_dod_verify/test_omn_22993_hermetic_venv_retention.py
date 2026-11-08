# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-22993: building a hermetic verify env prunes the stale ones beside it.

Measured on a lab host root filesystem: 39 lock-exact environments of about
0.5 GB each (20 GB) sat under the hermetic root after five days, because the
path is keyed by project root + lock bytes (one per lane worktree path) and
nothing ever removed one. That took the host under its free-disk floor.

The control: a stale environment beside a freshly built one must be gone
after the build, a recently used one must stay, and the one just built must
never be touched. Red on the base (nothing prunes), green with the fix.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from omnimarket.nodes.node_dod_verify.services import (
    evidence_collector as ec_mod,
)
from omnimarket.nodes.node_dod_verify.services.evidence_collector import (
    EvidenceCollector,
)

_RETENTION_ENV = "DOD_VERIFY_HERMETIC_VENV_RETENTION_S"
_HOUR = 3600


def _fake_env(root: Path, name: str, age_s: float) -> Path:
    env = root / name
    (env / "bin").mkdir(parents=True)
    (env / "bin" / "python").write_text("")
    (env / "lib.bin").write_bytes(b"x" * 1024)
    stamp = time.time() - age_s
    os.utime(env, (stamp, stamp))
    return env


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    envs = tmp_path / "hermetic-envs"
    envs.mkdir()
    monkeypatch.setenv(ec_mod._HERMETIC_VENV_ROOT_ENV, str(envs))
    monkeypatch.setenv(_RETENTION_ENV, str(6 * _HOUR))

    def fake_sync(
        argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        target = Path(str(kwargs["env"][ec_mod._UV_PROJECT_ENVIRONMENT_ENV]))  # type: ignore[index]
        (target / "bin").mkdir(parents=True, exist_ok=True)
        (target / "bin" / "python").write_text("")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(ec_mod.subprocess, "run", fake_sync)
    return envs


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='x'\nversion='0'\n")
    (project / "uv.lock").write_text("version = 1\n")
    return project


def test_building_an_env_removes_stale_siblings_and_keeps_recent_ones(
    root: Path, tmp_path: Path
) -> None:
    stale = _fake_env(root, "omnimarket-stale0000000", age_s=48 * _HOUR)
    recent = _fake_env(root, "omnimarket-recent000000", age_s=1 * _HOUR)

    path, err = EvidenceCollector()._ensure_hermetic_uv_env(_project(tmp_path))

    assert err is None
    assert path is not None
    assert (path / "bin" / "python").is_file()
    assert not stale.exists(), "a 48 h old environment must be pruned"
    assert recent.exists(), "an environment used within the retention must stay"


def test_a_reused_env_is_marked_used_so_it_outlives_the_retention(
    root: Path, tmp_path: Path
) -> None:
    project = _project(tmp_path)
    first, _ = EvidenceCollector()._ensure_hermetic_uv_env(project)
    assert first is not None
    old = time.time() - 48 * _HOUR
    os.utime(first, (old, old))

    again, _ = EvidenceCollector()._ensure_hermetic_uv_env(project)

    assert again == first
    assert first.exists()
    assert time.time() - first.stat().st_mtime < _HOUR


def test_non_positive_retention_disables_nothing_it_falls_back_to_default(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(_RETENTION_ENV, "0")
    fresh = _fake_env(root, "omnimarket-fresh0000000", age_s=1 * _HOUR)

    EvidenceCollector()._ensure_hermetic_uv_env(_project(tmp_path))

    assert fresh.exists()
