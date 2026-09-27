# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""run_git starts no detached auto maintenance (OMN-19845).

Every commit, fetch and receive-pack starts ``git maintenance run --auto
--detach``. The detached child creates and removes ``objects/maintenance.lock``
after the foreground command has already returned, so a caller that removes
its temporary clone right after a commit races it: ``shutil.rmtree`` raises
``OSError: [Errno 39] Directory not empty`` on ``.git/objects`` (observed in
omnimarket shadow run 36275499729, from the emitter's ``occ-batch-drop-*``
temporary directory). The OCC git transport clones into short-lived
directories and removes them straight after, so it must never start one.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.occ_git_transport import run_git

_MAINTENANCE_SPAWN = "maintenance run"
_FORCE_MAINTENANCE = ("git", "-c", "maintenance.auto=true")


def _init_repo(path: Path) -> Path:
    path.mkdir()
    env = scrub_git_location_env(os.environ)
    for argv in (
        ["git", "init", "-q"],
        ["git", "config", "user.name", "test"],
        ["git", "config", "user.email", "test@example.com"],
    ):
        subprocess.run(argv, cwd=path, check=True, capture_output=True, env=env)
    return path


@pytest.mark.unit
def test_commit_through_run_git_starts_no_auto_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")

    # Positive control: the same commit outside run_git, with auto maintenance
    # forced on, is traced starting a maintenance run. Without this the zero
    # below could come from a trace that records nothing.
    control_trace = tmp_path / "control.trace"
    subprocess.run(
        [*_FORCE_MAINTENANCE, "commit", "-q", "--allow-empty", "-m", "control"],
        cwd=repo,
        check=True,
        capture_output=True,
        env={**scrub_git_location_env(os.environ), "GIT_TRACE": str(control_trace)},
    )
    assert _MAINTENANCE_SPAWN in control_trace.read_text(encoding="utf-8")

    trace = tmp_path / "run_git.trace"
    monkeypatch.setenv("GIT_TRACE", str(trace))
    run_git(["git", "commit", "-q", "--allow-empty", "-m", "subject"], cwd=str(repo))

    traced = trace.read_text(encoding="utf-8")
    assert "commit" in traced
    assert _MAINTENANCE_SPAWN not in traced


@pytest.mark.unit
def test_run_git_keeps_caller_config_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _init_repo(tmp_path / "repo")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "omnimarket.probe")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "kept")

    assert (
        run_git(["git", "config", "--get", "omnimarket.probe"], cwd=str(repo)) == "kept"
    )
    assert (
        run_git(["git", "config", "--get", "maintenance.auto"], cwd=str(repo))
        == "false"
    )
    # The caller's own environment is not mutated by the call.
    assert os.environ["GIT_CONFIG_COUNT"] == "1"
