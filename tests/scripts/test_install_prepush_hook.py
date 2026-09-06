# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Actual-Git boundary tests for the immutable OMN-17851 pre-push bootstrap."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.hooks import install_prepush_hook as installer

pytestmark = pytest.mark.unit


def _run(*args: str, cwd: Path, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, cwd=cwd, check=False, capture_output=True, text=True, **kwargs
    )


def _git(repo: Path, *args: str) -> str:
    result = _run("git", *args, cwd=repo)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _write_fixture_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: committed-content-check\n"
        "        name: committed content check\n"
        "        entry: bash validate.sh\n"
        "        language: system\n"
        "        pass_filenames: false\n"
        "        always_run: true\n"
        "        stages: [pre-push]\n",
        encoding="utf-8",
    )
    (repo / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (repo / "subject.py").write_text("state = 'base'\n", encoding="utf-8")
    (repo / "validate.sh").write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'printf \'%s|%s\\n\' "$PWD" "$(cat subject.py)" > "$PREPUSH_RECORD"\n'
        "if grep -q bad subject.py; then exit 19; fi\n",
        encoding="utf-8",
    )
    (repo / "validate.sh").chmod(0o755)
    _commit(repo, "base")
    return repo


def _prepared_cache(repo: Path, sha: str, cache_root: Path) -> Path:
    entry = cache_root / sha
    tree = entry / "tree"
    venv_python = entry / "venv" / "bin" / "python"
    tree.parent.mkdir(parents=True)
    _git(repo, "worktree", "add", "--detach", str(tree), sha)
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(Path(sys.executable))
    (entry / "manifest.json").write_text(
        json.dumps(
            installer._manifest_data(tree=tree, local_sha=sha, venv=entry / "venv")
        )
        + "\n",
        encoding="utf-8",
    )
    return entry


def _run_bootstrap(
    hook: Path, repo: Path, refs: str, cache_root: Path, record: Path
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PREPUSH_SNAPSHOT_CACHE_ROOT"] = str(cache_root)
    env["PREPUSH_RECORD"] = str(record)
    env["PYTHONPATH"] = "/must-not-leak"
    env["VIRTUAL_ENV"] = "/must-not-leak"
    return subprocess.run(
        [str(hook), "origin", "https://example.invalid/repo.git"],
        cwd=repo,
        input=refs,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _install(repo: Path) -> Path:
    return installer.install(cwd=repo)


def test_precommit_python_preserves_virtualenv_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = Path(sys.executable)
    assert launcher.is_symlink()
    monkeypatch.setattr(installer.shutil, "which", lambda _name: None)

    assert installer._precommit_python() == str(launcher)


def test_committed_sha_is_checked_when_staged_and_unstaged_edits_mask_it(
    tmp_path: Path,
) -> None:
    repo = _write_fixture_repo(tmp_path)
    bad_sha: str
    (repo / "subject.py").write_text("state = 'bad'\n", encoding="utf-8")
    bad_sha = _commit(repo, "committed bad")
    cache_root = tmp_path / "cache"
    _prepared_cache(repo, bad_sha, cache_root)
    hook = _install(repo)

    # Deliberate concurrent author mutation after local_sha was chosen.
    (repo / "subject.py").write_text("state = 'good staged'\n", encoding="utf-8")
    _git(repo, "add", "subject.py")
    (repo / "subject.py").write_text("state = 'good unstaged'\n", encoding="utf-8")
    before = (
        _git(repo, "diff", "--binary"),
        _git(repo, "diff", "--cached", "--binary"),
        _git(repo, "status", "--porcelain=v1"),
    )
    record = tmp_path / "record.txt"
    result = _run_bootstrap(
        hook,
        repo,
        f"refs/heads/topic {bad_sha} refs/heads/topic {'0' * 40}\n",
        cache_root,
        record,
    )

    assert result.returncode == 1, result.stderr
    assert "committed-content-check" in result.stdout
    assert record.exists(), f"{result.stdout}\n{result.stderr}"
    assert "state = 'bad'" in record.read_text(encoding="utf-8")
    after = (
        _git(repo, "diff", "--binary"),
        _git(repo, "diff", "--cached", "--binary"),
        _git(repo, "status", "--porcelain=v1"),
    )
    assert after == before
    assert not list(tmp_path.glob("onex-prepush-snapshot.*"))


def test_no_selected_ref_is_a_noop_without_all_files_fallback(tmp_path: Path) -> None:
    repo = _write_fixture_repo(tmp_path)
    hook = _install(repo)
    record = tmp_path / "record.txt"

    result = _run_bootstrap(
        hook,
        repo,
        f"refs/heads/deleted {'0' * 40} refs/heads/deleted {'0' * 40}\n",
        tmp_path / "missing-cache",
        record,
    )

    assert result.returncode == 0, result.stderr
    assert not record.exists()


def test_missing_prepared_target_fails_closed_without_mutating_caller(
    tmp_path: Path,
) -> None:
    repo = _write_fixture_repo(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD")
    (repo / "subject.py").write_text("uncommitted\n", encoding="utf-8")
    before = _git(repo, "diff", "--binary")
    hook = _install(repo)

    result = _run_bootstrap(
        hook,
        repo,
        f"refs/heads/topic {sha} refs/heads/topic {'0' * 40}\n",
        tmp_path / "missing-cache",
        tmp_path / "record.txt",
    )

    assert result.returncode == 1
    assert "prepared target environment unavailable" in result.stderr
    assert _git(repo, "diff", "--binary") == before


@pytest.mark.parametrize("leading_ref", [True, False])
def test_precommit_parser_selects_later_ref_and_root_all_files(
    tmp_path: Path, leading_ref: bool
) -> None:
    repo = _write_fixture_repo(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD")
    cache_root = tmp_path / "cache"
    _prepared_cache(repo, sha, cache_root)
    hook = _install(repo)
    record = tmp_path / "record.txt"
    refs = (
        f"refs/heads/delete {'0' * 40} refs/heads/delete {'0' * 40}\n"
        f"refs/heads/topic {sha} refs/heads/topic {'0' * 40}\n"
        if leading_ref
        else f"refs/heads/topic {sha} refs/heads/topic {'0' * 40}\n"
    )

    result = _run_bootstrap(hook, repo, refs, cache_root, record)

    assert result.returncode == 0, result.stderr
    assert "state = 'base'" in record.read_text(encoding="utf-8")


def test_corrupt_manifest_fails_closed_without_mutating_caller(tmp_path: Path) -> None:
    repo = _write_fixture_repo(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD")
    cache_root = tmp_path / "cache"
    entry = _prepared_cache(repo, sha, cache_root)
    (entry / "manifest.json").write_text(
        json.dumps({"sha": "0" * 40, "lock_sha256": "0" * 64}) + "\n",
        encoding="utf-8",
    )
    (repo / "subject.py").write_text("author edit\n", encoding="utf-8")
    before = _git(repo, "diff", "--binary")
    hook = _install(repo)

    result = _run_bootstrap(
        hook,
        repo,
        f"refs/heads/topic {sha} refs/heads/topic {'0' * 40}\n",
        cache_root,
        tmp_path / "record.txt",
    )

    assert result.returncode == 1
    assert (
        "prepared environment identity does not match committed target" in result.stderr
    )
    assert _git(repo, "diff", "--binary") == before


def test_prepare_reports_a_corrupt_existing_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _write_fixture_repo(tmp_path)
    sha = _git(repo, "rev-parse", "HEAD")
    cache_root = tmp_path / "cache"
    entry = _prepared_cache(repo, sha, cache_root)
    (entry / "manifest.json").write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("PREPUSH_SNAPSHOT_CACHE_ROOT", str(cache_root))

    with pytest.raises(
        installer.HookInstallError, match="cannot read prepared cache manifest"
    ):
        installer.prepare(cwd=repo, local_sha=sha)
