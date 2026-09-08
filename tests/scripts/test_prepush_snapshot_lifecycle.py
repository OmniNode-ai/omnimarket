# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Lifecycle proofs for caller isolation and signal cleanup."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.hooks import install_prepush_hook as installer
from tests.scripts.test_prepush_snapshot_provenance import _fixture_repo, _git

pytestmark = pytest.mark.unit


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _make_validator(repo: Path, body: str) -> None:
    (repo / "scripts" / "provenance.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n" + body + "\n", encoding="utf-8"
    )
    (repo / "scripts" / "provenance.sh").chmod(0o755)


def _prepare_hook(
    repo: Path, sha: str, cache: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    monkeypatch.setenv("PREPUSH_SNAPSHOT_CACHE_ROOT", str(cache))
    installer.prepare(cwd=repo, local_sha=sha)
    return installer.install(cwd=repo)


def _start_hook(
    hook: Path,
    repo: Path,
    sha: str,
    cache: Path,
    record: Path,
    start_sentinel: Path | None = None,
) -> subprocess.Popen[str]:
    env = {
        **os.environ,
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ.get('PATH', '')}",
        "PREPUSH_SNAPSHOT_CACHE_ROOT": str(cache),
        "PREPUSH_RECORD": str(record),
        "PYTHONPATH": str(repo / "caller-shadow"),
        "UV_OFFLINE": "1",
        "UV_INDEX_URL": "https://invalid.example.invalid/simple",
    }
    if start_sentinel is not None:
        env["PREPUSH_START_SENTINEL"] = str(start_sentinel)
    return subprocess.Popen(
        [str(hook), "origin", "unused"],
        cwd=repo,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def _wait_for_worktree(repo: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if "onex-prepush-" in _git(repo, "worktree", "list", "--porcelain"):
            return
        time.sleep(0.05)
    raise AssertionError("owned pre-push snapshot was never registered")


def _wait_for_sentinel(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError("validator start sentinel was never written")


def test_snapshot_validation_ignores_concurrent_amend_and_unstaged_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _caller = _fixture_repo(tmp_path)
    (repo / "README.md").write_text("snapshot\n", encoding="utf-8")
    _make_validator(
        repo,
        ': > "$PREPUSH_START_SENTINEL"\n'
        "sleep 2\n"
        '"$UV_PROJECT_ENVIRONMENT/bin/python" -c '
        "'import subprocess; import targetpkg; "
        'sha=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(); '
        'content=subprocess.check_output(["git", "show", "HEAD:README.md"], text=True).strip(); '
        'print(sha + "|" + targetpkg.PROVENANCE + "|" + content)\''
        ' > "$PREPUSH_RECORD"',
    )
    original_sha = _commit(repo, "slow validator")
    cache = tmp_path / "cache"
    hook = _prepare_hook(repo, original_sha, cache, monkeypatch)
    record = tmp_path / "record.txt"
    process = _start_hook(
        hook,
        repo,
        original_sha,
        cache,
        record,
        tmp_path / "validator-started",
    )
    assert process.stdin is not None
    process.stdin.write(
        f"refs/heads/topic {original_sha} refs/heads/topic {'0' * 40}\n"
    )
    process.stdin.close()

    _wait_for_sentinel(tmp_path / "validator-started")
    (repo / "README.md").write_text("amended committed\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "--amend", "--no-edit")
    (repo / "README.md").write_text("retained unstaged\n", encoding="utf-8")
    amended_sha = _git(repo, "rev-parse", "HEAD")
    expected_unstaged = _git(repo, "diff", "--binary")
    expected_staged = _git(repo, "diff", "--cached", "--binary")
    expected_status = _git(repo, "status", "--porcelain=v1")

    process.wait(timeout=15)
    stdout = process.stdout.read() if process.stdout is not None else ""
    stderr = process.stderr.read() if process.stderr is not None else ""
    assert process.returncode == 0, f"{stdout}\n{stderr}"
    assert (
        record.read_text(encoding="utf-8").strip()
        == f"{original_sha}|committed-target|snapshot"
    )
    assert amended_sha != original_sha
    assert _git(repo, "show", "HEAD:README.md") == "amended committed"
    assert (repo / "README.md").read_text(
        encoding="utf-8"
    ).strip() == "retained unstaged"
    assert _git(repo, "diff", "--binary") == expected_unstaged
    assert _git(repo, "diff", "--cached", "--binary") == expected_staged
    assert _git(repo, "status", "--porcelain=v1") == expected_status
    assert "onex-prepush-" not in _git(repo, "worktree", "list", "--porcelain")


def test_sigterm_cleans_owned_snapshot_and_preserves_caller_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _caller = _fixture_repo(tmp_path)
    (repo / "README.md").write_text("snapshot\n", encoding="utf-8")
    _make_validator(repo, ': > "$PREPUSH_START_SENTINEL"\nsleep 30')
    sha = _commit(repo, "interruptible validator")
    (repo / "README.md").write_text("staged before signal\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    (repo / "README.md").write_text("unstaged before signal\n", encoding="utf-8")
    before = (
        _git(repo, "diff", "--binary"),
        _git(repo, "diff", "--cached", "--binary"),
        _git(repo, "status", "--porcelain=v1"),
    )
    cache = tmp_path / "cache"
    hook = _prepare_hook(repo, sha, cache, monkeypatch)
    process = _start_hook(
        hook,
        repo,
        sha,
        cache,
        tmp_path / "record.txt",
        tmp_path / "validator-started",
    )
    assert process.stdin is not None
    process.stdin.write(f"refs/heads/topic {sha} refs/heads/topic {'0' * 40}\n")
    process.stdin.close()
    _wait_for_sentinel(tmp_path / "validator-started")
    os.killpg(process.pid, signal.SIGTERM)
    process.wait(timeout=15)
    stdout = process.stdout.read() if process.stdout is not None else ""
    stderr = process.stderr.read() if process.stderr is not None else ""

    assert process.returncode is not None
    assert process.returncode != 0, f"{stdout}\n{stderr}"
    assert (
        _git(repo, "diff", "--binary"),
        _git(repo, "diff", "--cached", "--binary"),
        _git(repo, "status", "--porcelain=v1"),
    ) == before
    assert "onex-prepush-" not in _git(repo, "worktree", "list", "--porcelain")
