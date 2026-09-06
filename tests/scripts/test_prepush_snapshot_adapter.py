# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Focused contract tests for the immutable pre-push adapter CLI."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.hooks import prepush_snapshot_adapter as adapter

pytestmark = pytest.mark.unit


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


def _git(repo: Path, *args: str) -> str:
    result = _run("git", *args, cwd=repo)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("snapshot\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def _manifest(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    expected = {
        "sha": "a" * 40,
        "lock_sha256": "b" * 64,
        "config_sha256": "c" * 64,
        "python_abi": "cpython-313",
        "platform": "macos-aarch64",
        "uv_version": "0.11.31",
        "pre_commit_version": "4.5.1",
    }
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps({**expected, "policy": adapter.POLICY}) + "\n", encoding="utf-8"
    )
    return path, expected


def test_manifest_cli_rejects_each_identity_field_and_corrupt_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest_path, expected = _manifest(tmp_path)
    cli_args = ["validate-manifest", "--manifest", str(manifest_path)]
    for key, value in expected.items():
        cli_args.extend((f"--{key.replace('_', '-')}", value))
    assert adapter.main(cli_args) == 0
    assert capsys.readouterr().err == ""
    # Each field is independently part of the identity contract; changing one
    # must invalidate the prepared target even when every other field matches.
    for key in expected:
        altered = dict(expected)
        altered[key] = "wrong"
        assert adapter.validate(manifest_path, altered)
    manifest_path.write_text("not-json\n", encoding="utf-8")
    assert adapter.validate(manifest_path, expected)


def test_marker_identity_and_owned_cleanup_require_registered_worktree(
    tmp_path: Path,
) -> None:
    repo, sha = _repo(tmp_path)
    parent = tmp_path / "onex-prepush-owned"
    snapshot = parent / "tree"
    _git(repo, "worktree", "add", "--detach", str(snapshot), sha)
    adapter.write_marker(snapshot, sha)

    assert adapter.check_marker(snapshot, repo) == 0

    marker = snapshot / adapter.MARKER_NAME
    data = json.loads(marker.read_text(encoding="utf-8"))
    data["adapter_sha256"] = "0" * 64
    marker.write_text(json.dumps(data), encoding="utf-8")
    assert adapter.check_marker(snapshot, repo) == 1

    # A caller-controlled parent cannot cause removal of the registered tree.
    assert adapter.cleanup(repo, snapshot, tmp_path / "not-owned") == 1
    assert snapshot.is_dir()

    adapter.write_marker(snapshot, sha)
    assert adapter.cleanup(repo, snapshot, parent) == 0
    assert not snapshot.exists()
    assert not parent.exists()
    assert str(snapshot) not in _git(repo, "worktree", "list", "--porcelain")
