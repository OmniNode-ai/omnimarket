# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Clone parity diagnostics and rendered-script coverage (OMN-21328)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_clone_parity_compute.handlers.handler_clone_parity import (
    CLONE_PARITY_PY,
    git_identity_failure,
    parse_git_identity,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep fixture repositories independent of inherited Git state and identity."""
    for name in subprocess.check_output(
        ["git", "rev-parse", "--local-env-vars"],
        text=True,
        env=scrub_git_location_env(os.environ),
    ).splitlines():
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")


def test_parse_git_identity_uses_last_line() -> None:
    assert parse_git_identity("PARITY-GITIDENT 1 0\nother\nPARITY-GITIDENT 0 1\n") == (
        False,
        True,
    )
    assert parse_git_identity("PARITY-GITIDENT 1 1\n") == (True, True)
    assert parse_git_identity("PARITY-HOME registry\n") is None


def test_git_identity_failure_names_unset_email() -> None:
    assert git_identity_failure(True, True) is None
    failure = git_identity_failure(True, False)
    assert failure is not None
    assert failure[0] == "git-identity"
    assert "user.email" in failure[1]
    assert "user.name" not in failure[1]


def test_clone_parity_script_reads_registry_and_missing_clone(tmp_path: Path) -> None:
    root = tmp_path / "registry"
    subprocess.run(
        ["git", "init", "-q", str(root / "repo-a")],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    prelude = """import os,subprocess,sys
root=sys.argv[1]
clones='repo-a,repo-missing'
def git(path,*args):
    done=subprocess.run(['git','-C',path,*args],capture_output=True,text=True,check=False)
    return done.stdout.strip() if done.returncode==0 else None
def clean(value):
    return value.replace('\\n',' ').replace('\\t',' ')
"""
    done = subprocess.run(
        [sys.executable, "-c", prelude + CLONE_PARITY_PY, str(root)],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env={
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
    )
    lines = done.stdout.splitlines()
    assert f"PARITY-HOME {root}" in lines
    assert any(line.startswith("PARITY-CLONE repo-a present ") for line in lines)
    assert "PARITY-CLONE repo-missing missing - - -" in lines
    assert "PARITY-GITIDENT 0 0" in lines
