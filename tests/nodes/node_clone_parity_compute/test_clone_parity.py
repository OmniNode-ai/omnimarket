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
    PROVISION_FAST_FORWARD_SH,
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


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *(["-C", str(cwd)] if cwd else []), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
        env={
            **scrub_git_location_env(os.environ),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    ).stdout.strip()


def _clone_cut_while_main_was_default(tmp_path: Path, remote_default: str) -> Path:
    """A clone on ``dev`` whose origin/HEAD still names ``main``; the remote's default is now ``remote_default``."""
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    _git("init", "-q", "--bare", "-b", "main", str(remote))
    _git("init", "-q", "-b", "main", str(seed))
    (seed / "a.txt").write_text("a\n")
    _git("add", "a.txt", cwd=seed)
    _git("commit", "-q", "-m", "main", cwd=seed)
    _git("push", "-q", str(remote), "main", cwd=seed)
    clone = tmp_path / "clone"
    _git("clone", "-q", str(remote), str(clone))
    # The raw url is the GitHub one the provision script keys on; the rewrite sends fetches to the fixture remote.
    _git(
        "config",
        "remote.origin.url",
        "git@github.com:OmniNode-ai/fixture.git",
        cwd=clone,
    )
    _git(
        "config",
        f"url.{remote}.insteadOf",
        "git@github.com:OmniNode-ai/fixture.git",
        cwd=clone,
    )
    _git("switch", "-q", "-c", "dev", cwd=seed)
    (seed / "b.txt").write_text("b\n")
    _git("add", "b.txt", cwd=seed)
    _git("commit", "-q", "-m", "dev", cwd=seed)
    _git("push", "-q", str(remote), "dev", cwd=seed)
    _git("symbolic-ref", "HEAD", f"refs/heads/{remote_default}", cwd=remote)
    _git("fetch", "-q", "origin", cwd=clone)
    _git("switch", "-q", "--track", "-c", "dev", "origin/dev", cwd=clone)
    assert (
        _git("symbolic-ref", "refs/remotes/origin/HEAD", cwd=clone)
        == "refs/remotes/origin/main"
    )
    return clone


def _provision_fast_forward(clone: Path) -> str:
    script = (
        'fail() { printf "FAIL %s %s %s\\n" "$1" "$2" "$3"; }\n'
        f'n=repo; D="{clone}"; NEW=0\n' + PROVISION_FAST_FORWARD_SH
    )
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        env={
            **scrub_git_location_env(os.environ),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
        },
    ).stdout


def test_provision_fast_forward_reads_the_remote_default_not_a_stale_origin_head(
    tmp_path: Path,
) -> None:
    """OMN-20266: a clone cut while main was the default stayed refused on dev after the remote default became dev."""
    clone = _clone_cut_while_main_was_default(tmp_path, remote_default="dev")
    out = _provision_fast_forward(clone)
    assert "FAIL" not in out
    assert "PROVISIONED clone:repo fast-forward" in out
    assert (
        _git("symbolic-ref", "refs/remotes/origin/HEAD", cwd=clone)
        == "refs/remotes/origin/dev"
    )


def test_provision_fast_forward_still_refuses_a_branch_that_is_not_the_remote_default(
    tmp_path: Path,
) -> None:
    clone = _clone_cut_while_main_was_default(tmp_path, remote_default="main")
    out = _provision_fast_forward(clone)
    assert (
        "FAIL clone:repo fast-forward checked out dev, not the default branch main"
        in out
    )
    assert "PROVISIONED" not in out
