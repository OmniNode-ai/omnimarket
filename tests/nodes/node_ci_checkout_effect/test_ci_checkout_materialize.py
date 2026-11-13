# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for CI checkout materialization over git."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_ci_checkout_effect.handlers.adapter_ci_checkout_materialize import (
    CheckoutNotServedError,
    materialize,
)
from omnimarket.nodes.node_ci_checkout_effect.models.model_ci_checkout import (
    EnumCiCheckoutStatus,
    ModelCiCheckoutResult,
)

pytestmark = pytest.mark.unit


def _make_bare_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """Create a bare git repo with two commits, return (path, old_sha, new_sha)."""
    work = tmp_path / "work"
    work.mkdir()
    bare = tmp_path / "mirror.git"

    def _git(*args: str, cwd: Path | None = None) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or work,
            capture_output=True,
            text=True,
            check=True,
            env=scrub_git_location_env(os.environ),
        )
        return result.stdout.strip()

    _git("init", "-q", cwd=work)
    _git("config", "user.email", "test@example.com", cwd=work)
    _git("config", "user.name", "Test", cwd=work)
    (work / "a.txt").write_text("first\n")
    _git("add", "a.txt", cwd=work)
    _git("commit", "-q", "-m", "first", cwd=work)
    old_sha = _git("rev-parse", "HEAD", cwd=work)
    (work / "b.txt").write_text("second\n")
    _git("add", "b.txt", cwd=work)
    _git("commit", "-q", "-m", "second", cwd=work)
    new_sha = _git("rev-parse", "HEAD", cwd=work)
    _git("clone", "-q", "--bare", str(work), str(bare))
    return bare, old_sha, new_sha


def test_materialize_at_old_sha(tmp_path: Path) -> None:
    bare, old_sha, _ = _make_bare_repo(tmp_path)
    dest = tmp_path / "checkout"
    result = ModelCiCheckoutResult(
        correlation_id=uuid4(),
        host="test",
        status=EnumCiCheckoutStatus.SERVED,
        repo="owner/repo",
        sha=old_sha,
        source_name="mirror",
        serve_url=f"file://{bare}",
        upstream_fetches=0,
        detail=None,
    )
    materialize(result, dest, git_timeout_seconds=30)
    head = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()
    assert head == old_sha
    assert not (dest / "b.txt").exists()


def test_materialize_refuses_not_served(tmp_path: Path) -> None:
    result = ModelCiCheckoutResult(
        correlation_id=uuid4(),
        host="test",
        status=EnumCiCheckoutStatus.NOT_FOUND,
        repo="owner/repo",
        sha="a" * 40,
        source_name=None,
        serve_url=None,
        upstream_fetches=0,
        detail=None,
    )
    with pytest.raises(CheckoutNotServedError):
        materialize(result, tmp_path / "x", git_timeout_seconds=30)


@pytest.fixture
def github_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every route to GitHub is dead: proxies point at a closed port and github.com is rewritten to it."""
    for name in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "url.http://127.0.0.1:9/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "https://github.com/")


def test_github_unreachable_proof(tmp_path: Path, github_blocked: None) -> None:
    """With GitHub blocked the checkout from the served mirror still lands, with no remote configured."""
    bare, old_sha, _ = _make_bare_repo(tmp_path)
    dest = tmp_path / "checkout"
    result = ModelCiCheckoutResult(
        correlation_id=uuid4(),
        host="test",
        status=EnumCiCheckoutStatus.SERVED,
        repo="owner/repo",
        sha=old_sha,
        source_name="mirror",
        serve_url=f"file://{bare}",
        upstream_fetches=0,
    )
    materialize(result, dest, git_timeout_seconds=30)
    assert (dest / "a.txt").read_text() == "first\n"
    remotes = subprocess.run(
        ["git", "-C", str(dest), "remote"],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    ).stdout.strip()
    assert remotes == ""


def test_failing_control_the_github_fetch_actions_checkout_makes_fails(
    tmp_path: Path, github_blocked: None
) -> None:
    """The same blocked environment fails the fetch a plain checkout makes: the proof above is not a quiet network."""
    work = tmp_path / "plain"
    subprocess.run(
        ["git", "init", "-q", str(work)],
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(work),
            "fetch",
            "-q",
            "--depth=1",
            "https://github.com/OmniNode-ai/omnimarket.git",
            "a" * 40,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        env=scrub_git_location_env(os.environ),
    )
    assert proc.returncode != 0
    assert (
        "127.0.0.1" in proc.stderr
        or "proxy" in proc.stderr.lower()
        or "connect" in proc.stderr.lower()
    )
