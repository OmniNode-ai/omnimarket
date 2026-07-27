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
    """Rule #6: no local absolute path prefixes in the wrapper."""
    text = WRAPPER.read_text()
    forbidden_prefixes = ["/" + "Users/", "/" + "Volumes/"]
    for prefix in forbidden_prefixes:
        assert prefix not in text, (
            f"hardcoded local absolute path in merge-proof: {prefix}"
        )


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
    fake_home = tmp_path / "omni home; echo bad"
    fake_home.mkdir()
    fake_infra = tmp_path / "fake infra; echo bad"
    fake_infra.mkdir()
    env = _clean_env()
    env["OMNI_HOME"] = str(fake_home)
    env["OMNIBASE_INFRA_PATH"] = str(fake_infra)
    result = _run(isolated_wrapper, "--print-env", env=env)
    assert result.returncode == 0
    eval_result = subprocess.run(
        [
            "bash",
            "-c",
            (
                f"{result.stdout}\n"
                'printf "%s\\n%s\\n" "$OMNI_HOME" "$OMNIBASE_INFRA_PATH"'
            ),
        ],
        env=_clean_env(),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert eval_result.returncode == 0, eval_result.stderr
    assert eval_result.stdout.splitlines() == [str(fake_home), str(fake_infra)]


def test_delegated_command_does_not_inherit_git_worktree_bindings(
    isolated_wrapper: Path, tmp_path: Path
) -> None:
    fake_infra = tmp_path / "fakeinfra"
    fake_infra.mkdir()
    env = _clean_env()
    env.update(
        {
            "OMNIBASE_INFRA_PATH": str(fake_infra),
            "GIT_DIR": "/tmp/wrong.git",
            "GIT_INDEX_FILE": "/tmp/wrong.index",
            "GIT_WORK_TREE": "/tmp/wrong-worktree",
        }
    )
    result = _run(
        isolated_wrapper,
        "--",
        "bash",
        "-c",
        'printf "%s|%s|%s" "${GIT_DIR-unset}" "${GIT_INDEX_FILE-unset}" "${GIT_WORK_TREE-unset}"',
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "unset|unset|unset"


def test_omni_worktree_layout_autoresolves_without_env(tmp_path: Path) -> None:
    """From the mandated worktree layout the wrapper derives env with no exports.

    This is the ergonomic win: a correct change proves locally without the
    operator reconstructing hidden environment. The topology is built
    hermetically instead of relying on CI to have a sibling omnibase_infra
    checkout next to the test checkout.
    """
    fake_home = tmp_path / "omni_home"
    worktree_repo = fake_home / "omni_worktrees" / "OMN-14462" / "omnimarket"
    scripts = worktree_repo / "scripts"
    scripts.mkdir(parents=True)
    wrapper = scripts / "merge-proof"
    shutil.copy2(WRAPPER, wrapper)
    wrapper.chmod(0o755)

    fake_infra_validator = (
        fake_home / "omnibase_infra" / "scripts" / "validation" / "lint_topic_names.py"
    )
    fake_infra_validator.parent.mkdir(parents=True)
    fake_infra_validator.write_text("")

    result = _run(wrapper, "--check", env=_clean_env())
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    assert f"OMNI_HOME={fake_home}" in result.stdout
    assert f"OMNIBASE_INFRA_PATH={fake_home / 'omnibase_infra'}" in result.stdout
