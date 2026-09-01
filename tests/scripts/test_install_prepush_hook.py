# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Regression coverage for the shared-worktree pre-push bootstrap (OMN-17461)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scripts.hooks import install_prepush_hook as installer

pytestmark = pytest.mark.unit


def _run(*args: str, cwd: Path) -> str:
    return subprocess.run(
        args,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run("git", "init", cwd=repo)
    _run("git", "config", "user.email", "test@example.invalid", cwd=repo)
    _run("git", "config", "user.name", "Test", cwd=repo)
    (repo / ".pre-commit-config.yaml").write_text(
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: prepush-smart-tests\n"
        "        entry: bash scripts/hooks/prepush_smart_tests.sh\n"
        "        stages: [pre-push]\n",
        encoding="utf-8",
    )
    required_hook = repo / installer.LEGACY_HOOK_RELATIVE_PATH
    required_hook.parent.mkdir(parents=True)
    required_hook.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    required_hook.chmod(0o755)
    (repo / "README.md").write_text("fixture\n", encoding="utf-8")
    _run("git", "add", ".", cwd=repo)
    _run("git", "commit", "-m", "fixture", cwd=repo)
    return repo


def _write_precommit_stub(bin_dir: Path, record: Path) -> None:
    executable = bin_dir / "pre-commit"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'printf \'cwd=%s\\nargs=%s\\n\' "$PWD" "$*" > {record!s}\n',
        encoding="utf-8",
    )
    executable.chmod(0o755)


def test_bootstrap_runs_the_invoking_worktree_configuration(tmp_path: Path) -> None:
    """A newer canonical checkout cannot select policy for an older worktree."""
    canonical = _init_repo(tmp_path)
    stale = tmp_path / "stale-worktree"
    _run("git", "worktree", "add", "-b", "stale", str(stale), cwd=canonical)
    hook_path = installer.install(cwd=canonical)

    # This differs from the canonical config to make accidental canonical-root
    # selection observable without executing any real hooks.
    (stale / ".pre-commit-config.yaml").write_text(
        "repos:\n"
        "  - repo: local\n"
        "    hooks:\n"
        "      - id: prepush-smart-tests\n"
        "        entry: bash scripts/hooks/prepush_smart_tests.sh\n"
        "        stages: [pre-push]\n"
        "# stale\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "pre-commit-record.txt"
    _write_precommit_stub(bin_dir, record)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}

    result = subprocess.run(
        [str(hook_path), "origin", "unused"],
        cwd=stale,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    invocation = record.read_text(encoding="utf-8")
    assert f"cwd={stale}" in invocation
    assert f"--config {stale}/.pre-commit-config.yaml" in invocation
    assert "--hook-type pre-push" in invocation


def test_install_replaces_only_the_known_legacy_canonical_symlink(
    tmp_path: Path,
) -> None:
    repo = _init_repo(tmp_path)
    legacy_target = repo / installer.LEGACY_HOOK_RELATIVE_PATH
    legacy_target.parent.mkdir(parents=True, exist_ok=True)
    legacy_target.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    hook_path = installer.common_hook_path(cwd=repo)
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.symlink_to(legacy_target)

    installed = installer.install(cwd=repo)

    assert installed == hook_path
    assert not hook_path.is_symlink()
    assert hook_path.read_text(encoding="utf-8") == installer.BOOTSTRAP
    assert installer.check_installation(cwd=repo)[0]


def test_linked_worktree_replaces_primary_legacy_symlink(tmp_path: Path) -> None:
    """The real shared hook targets the primary checkout, never the linked one."""
    primary = _init_repo(tmp_path)
    linked = tmp_path / "linked-worktree"
    _run("git", "worktree", "add", "-b", "linked", str(linked), cwd=primary)

    legacy_target = primary / installer.LEGACY_HOOK_RELATIVE_PATH
    legacy_target.parent.mkdir(parents=True, exist_ok=True)
    legacy_target.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    hook_path = installer.common_hook_path(cwd=linked)
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.symlink_to(legacy_target)

    installed = installer.install(cwd=linked)

    assert installed == hook_path
    assert not hook_path.is_symlink()
    assert installer.check_installation(cwd=linked)[0]


def test_install_is_idempotent_and_refuses_unknown_hook(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    hook_path = installer.install(cwd=repo)
    before = hook_path.read_text(encoding="utf-8")
    assert installer.install(cwd=repo) == hook_path
    assert hook_path.read_text(encoding="utf-8") == before

    hook_path.write_text(
        f"#!/usr/bin/env bash\n# {installer.BOOTSTRAP_MARKER}\n# previous version\n",
        encoding="utf-8",
    )
    assert installer.install(cwd=repo) == hook_path
    assert hook_path.read_text(encoding="utf-8") == installer.BOOTSTRAP

    hook_path.write_text("#!/usr/bin/env bash\necho third-party\n", encoding="utf-8")
    with pytest.raises(installer.HookInstallError, match="refusing to replace unknown"):
        installer.install(cwd=repo)
    assert "third-party" in hook_path.read_text(encoding="utf-8")


def test_bootstrap_refuses_missing_invoking_worktree_config(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    hook_path = installer.install(cwd=repo)
    (repo / ".pre-commit-config.yaml").unlink()
    result = subprocess.run(
        [str(hook_path)],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "refusing to run a different worktree's pre-push policy" in result.stderr


def test_bootstrap_refuses_missing_governed_hook_authority(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    hook_path = installer.install(cwd=repo)
    (repo / installer.LEGACY_HOOK_RELATIVE_PATH).unlink()

    result = subprocess.run(
        [str(hook_path)],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "refusing to silently downgrade" in result.stderr


def test_bootstrap_refuses_missing_governed_pre_push_config(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    hook_path = installer.install(cwd=repo)
    (repo / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")

    result = subprocess.run(
        [str(hook_path)],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "refusing a vacuous pass" in result.stderr


def test_check_refuses_a_non_executable_bootstrap(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path)
    hook_path = installer.install(cwd=repo)
    hook_path.chmod(0o644)

    ok, message = installer.check_installation(cwd=repo)

    assert not ok
    assert "not executable" in message
