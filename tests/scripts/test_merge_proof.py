# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for scripts/merge-proof (OMN-14462 / F-20).

The wrapper resolves the environment omnimarket local proof gates need
(OMNI_HOME + OMNIBASE_INFRA_PATH) deterministically from its own on-disk
location, and — when it cannot — fails fast with the exact ``export`` lines the
operator must run, rather than proceeding on a silent wrong path.

These tests drive the real script via subprocess in an isolated tmp location so
derivation deterministically fails unless the environment is supplied. The git
env vars are stripped from the child environment per the OMN-14746/14744
worktree-safety lesson (the wrapper runs no git, but callers must never inherit
GIT_DIR/GIT_INDEX_FILE/GIT_WORK_TREE into subprocesses).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WRAPPER = REPO_ROOT / "scripts" / "merge-proof"


def _clean_env() -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "OMNI_HOME",
            "OMNIBASE_INFRA_PATH",
            "GIT_DIR",
            "GIT_INDEX_FILE",
            "GIT_WORK_TREE",
        }
    }
    return env


@pytest.fixture
def isolated_wrapper(tmp_path: Path) -> Path:
    """Copy the wrapper into a location where env derivation cannot succeed.

    Not under ``omni_worktrees/`` and with no sibling ``omnibase_infra``.
    """
    scripts = tmp_path / "isolated" / "scripts"
    scripts.mkdir(parents=True)
    dest = scripts / "merge-proof"
    shutil.copy2(WRAPPER, dest)
    dest.chmod(0o755)
    return dest


def _run(
    wrapper: Path, *args: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(wrapper), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_wrapper_exists_and_is_executable() -> None:
    assert WRAPPER.is_file(), f"missing wrapper: {WRAPPER}"
    assert os.access(WRAPPER, os.X_OK), "wrapper must be executable"


def test_no_hardcoded_absolute_paths() -> None:
    """Rule #6: no /Users/ or /Volumes/ absolute paths in the wrapper."""
    text = WRAPPER.read_text()
    assert "/Users/" not in text, "hardcoded /Users/ path in merge-proof"
    assert "/Volumes/" not in text, "hardcoded /Volumes/ path in merge-proof"


def test_unresolved_env_fails_with_export_guidance(isolated_wrapper: Path) -> None:
    """Env UNSET and no derivable sibling → non-zero exit AND exact export lines.

    Asserting on the *presence* of the guidance (not merely non-zero exit) so a
    silent wrong-path failure cannot pass this test.
    """
    result = _run(isolated_wrapper, "--check", env=_clean_env())
    assert result.returncode != 0, f"expected failure, got 0\nstdout={result.stdout}"
    combined = result.stdout + result.stderr
    assert "export OMNI_HOME=" in combined, combined
    assert "export OMNIBASE_INFRA_PATH=" in combined, combined


def test_resolved_env_check_passes(isolated_wrapper: Path, tmp_path: Path) -> None:
    """Env SET to a real omnibase_infra checkout → --check exits 0."""
    fake_infra = tmp_path / "fakeinfra"
    (fake_infra / "scripts" / "validation").mkdir(parents=True)
    (fake_infra / "scripts" / "validation" / "lint_topic_names.py").write_text("")
    env = _clean_env()
    env["OMNIBASE_INFRA_PATH"] = str(fake_infra)
    result = _run(isolated_wrapper, "--check", env=env)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "environment OK" in result.stdout


def test_check_fails_when_infra_lacks_validator(
    isolated_wrapper: Path, tmp_path: Path
) -> None:
    """OMNIBASE_INFRA_PATH points at a dir with no validator → non-zero exit."""
    empty_infra = tmp_path / "emptyinfra"
    empty_infra.mkdir()
    env = _clean_env()
    env["OMNIBASE_INFRA_PATH"] = str(empty_infra)
    result = _run(isolated_wrapper, "--check", env=env)
    assert result.returncode != 0
    assert "validator not found" in (result.stdout + result.stderr)


def test_print_env_emits_evalable_exports(
    isolated_wrapper: Path, tmp_path: Path
) -> None:
    fake_infra = tmp_path / "fakeinfra"
    fake_infra.mkdir()
    env = _clean_env()
    env["OMNIBASE_INFRA_PATH"] = str(fake_infra)
    result = _run(isolated_wrapper, "--print-env", env=env)
    assert result.returncode == 0
    assert f"export OMNIBASE_INFRA_PATH={fake_infra}" in result.stdout


def test_real_worktree_autoresolves_without_env() -> None:
    """From the real checkout the wrapper derives env with NOTHING exported.

    This is the ergonomic win: a correct change proves locally without the
    operator reconstructing hidden environment.
    """
    result = _run(WRAPPER, "--check", env=_clean_env())
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert "OMNIBASE_INFRA_PATH=" in result.stdout
