# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for scripts/ci/check_shell_hygiene.sh (OMN-14761 / F-22).

The gate is fail-closed and two-tier:
  scripts/lib/**  -> shellcheck --severity=style (strict, new canonical code)
  everything else -> shellcheck --severity=error (whole tree; zero baseline)

Each test builds a disposable git repo, copies the real gate script in, and runs
it — asserting against *deliberately broken* fixtures so a green-on-absence gate
cannot pass. Git is driven with GIT_DIR/GIT_INDEX_FILE/GIT_WORK_TREE stripped per
the OMN-14746/14744 worktree-safety lesson.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "ci" / "check_shell_hygiene.sh"

_CLEAN = '#!/usr/bin/env bash\nset -euo pipefail\nnow="$(date)"\necho "${now}"\n'
# Unterminated command substitution -> SC1073/SC1072 (error severity).
_ERROR = "#!/usr/bin/env bash\nfoo=$(\necho hi\n"
# Backticks -> SC2006 (style severity only; passes at --severity=error).
_STYLE = '#!/usr/bin/env bash\nset -euo pipefail\nnow=`date`\necho "$now"\n'

_HERMETIC_ENV = {
    k: v
    for k, v in os.environ.items()
    if k not in {"GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"}
}


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, env=_HERMETIC_ENV, capture_output=True, check=True
    )


def _gate_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """Build a disposable git repo containing the real gate + given fixtures."""
    repo = tmp_path / "repo"
    (repo / "scripts" / "ci").mkdir(parents=True)
    shutil.copy2(GATE, repo / "scripts" / "ci" / "check_shell_hygiene.sh")
    (repo / "scripts" / "ci" / "check_shell_hygiene.sh").chmod(0o755)
    for rel, content in files.items():
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)
        if rel.endswith(".sh") or content.startswith("#!"):
            dest.chmod(0o755)
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.com"], repo)
    _git(["config", "user.name", "T"], repo)
    _git(["add", "-A"], repo)
    return repo


def _run(
    repo: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "scripts/ci/check_shell_hygiene.sh", *args],
        cwd=repo,
        env=env if env is not None else _HERMETIC_ENV,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_gate_exists_and_executable() -> None:
    assert GATE.is_file()
    assert os.access(GATE, os.X_OK)


def test_clean_tree_passes(tmp_path: Path) -> None:
    repo = _gate_repo(tmp_path, {"scripts/clean.sh": _CLEAN})
    result = _run(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "clean" in result.stdout


def test_error_severity_finding_fails(tmp_path: Path) -> None:
    """A non-lib script with an error-level defect fails the gate."""
    repo = _gate_repo(tmp_path, {"scripts/broken.sh": _ERROR})
    result = _run(repo)
    assert result.returncode != 0, "gate must reject an error-severity defect"
    assert "broken.sh" in (result.stdout + result.stderr)


def test_style_only_finding_passes_for_non_lib(tmp_path: Path) -> None:
    """A non-lib script with only a style-level finding passes (error tier)."""
    repo = _gate_repo(tmp_path, {"scripts/legacy.sh": _STYLE})
    result = _run(repo)
    assert result.returncode == 0, (
        "non-lib scripts are gated at --severity=error; a style-only finding "
        f"must not fail.\n{result.stdout}\n{result.stderr}"
    )


def test_style_finding_fails_for_lib(tmp_path: Path) -> None:
    """The same style-level finding under scripts/lib/ DOES fail (strict tier)."""
    repo = _gate_repo(tmp_path, {"scripts/lib/helper.sh": _STYLE})
    result = _run(repo)
    assert result.returncode != 0, "scripts/lib/** is gated at --severity=style"
    assert "helper.sh" in (result.stdout + result.stderr)


def test_extensionless_shell_script_is_scanned(tmp_path: Path) -> None:
    """An extensionless shell script under scripts/ is discovered and checked."""
    repo = _gate_repo(tmp_path, {"scripts/merge-proof-like": _ERROR})
    result = _run(repo)
    assert result.returncode != 0, "extensionless shell scripts must be scanned"
    assert "merge-proof-like" in (result.stdout + result.stderr)


def test_file_args_mode_flags_broken_file(tmp_path: Path) -> None:
    """Pre-commit passes changed files as args; a broken one is rejected."""
    repo = _gate_repo(
        tmp_path, {"scripts/broken.sh": _ERROR, "scripts/clean.sh": _CLEAN}
    )
    result = _run(repo, "scripts/broken.sh")
    assert result.returncode != 0
    assert "broken.sh" in (result.stdout + result.stderr)


def test_fail_closed_when_shellcheck_missing(tmp_path: Path) -> None:
    """No shellcheck on PATH → gate fails closed (does not silently pass)."""
    repo = _gate_repo(tmp_path, {"scripts/clean.sh": _CLEAN})
    env = dict(_HERMETIC_ENV)
    env["PATH"] = "/usr/bin:/bin"  # git+bash present, shellcheck (brew) excluded
    which = subprocess.run(
        ["bash", "-c", "command -v shellcheck"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if which.returncode == 0:
        pytest.skip("shellcheck present on minimal PATH; cannot test fail-closed")
    result = _run(repo, env=env)
    assert result.returncode != 0
    assert "shellcheck" in (result.stdout + result.stderr).lower()
