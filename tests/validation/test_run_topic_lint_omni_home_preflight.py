# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Sibling-resolution fail-fast preflight for topic-naming-lint (OMN-17167).

``scripts/validation/run_topic_lint.sh`` runs the canonical linter, which lives in
``omnibase_infra``. Reported by a contractor 2026-08-30: with a stale or unset
sibling-resolution variable the hook printed nothing but a not-found path -- it
never named which variable was wrong, so from a worktree at
``$OMNI_HOME/omni_worktrees/<ticket>/omnimarket`` it was unrunnable and gave the
operator nothing to act on (the OMN-14444 mechanism).

OMN-17167 correction (2026-08-30): the first cut of this fix routed every
failure through a message that unconditionally named ``OMNI_HOME``, even though
``OMNIBASE_INFRA_PATH`` is this hook's own, higher-priority override -- checked
before ``$OMNI_HOME/omnibase_infra`` in the script's candidate list -- and
``OMNI_HOME`` here is only a worktree-friendly fallback. Blaming ``OMNI_HOME``
when the operator already set (a wrong) ``OMNIBASE_INFRA_PATH`` sends them to fix
the variable that ISN'T the problem. These tests assert the preflight names
whichever candidate variable was actually set-but-wrong, and only talks about
``OMNI_HOME`` as a fallback option when ``OMNIBASE_INFRA_PATH`` was never set.

Doctrine under test: omni_home CLAUDE.md rule 8 (fail fast on missing env, never a
silent default), rule 6 (no absolute paths -- the remediation is an ``export``
line), and memory ``feedback_own_errors_give_full_paths`` (name the actual
variable AND the full missing path).

These tests drive THE real script end-to-end via subprocess from an isolated tmp
cwd where no sibling can resolve -- the same harness shape as
``tests/scripts/test_merge_proof.py``. Git env vars are stripped from the child per
the OMN-14746/14744 worktree-safety lesson.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "validation" / "run_topic_lint.sh"

LINT_REL = Path("scripts") / "validation" / "lint_topic_names.py"

# The literal remediation the operator must be able to copy. Asserting on its
# PRESENCE (not merely a non-zero exit) is the point of the ticket: an opaque
# non-zero exit is the defect, not the fix.
_BOTH_UNSET_MESSAGE = "Neither OMNIBASE_INFRA_PATH nor OMNI_HOME is set."
_UNSET_EXPORT_EXAMPLE = (
    "export OMNIBASE_INFRA_PATH=<path to the omnibase_infra repo root>"
)
_INFRA_PATH_STALE_MESSAGE = "OMNIBASE_INFRA_PATH is set to"
_OMNI_HOME_STALE_MESSAGE = "OMNI_HOME is set to"
_OMNI_HOME_FALLBACK_PREFIX = "OMNIBASE_INFRA_PATH is not set, and OMNI_HOME is set to"

_STUB_LINTER = """#!/usr/bin/env python3
import sys
print("STUB_LINTER_INVOKED " + " ".join(sys.argv[1:]))
"""

# The script prefers ``$OMNIBASE_INFRA/.venv/bin/python`` when that interpreter can
# ``import yaml``. Planting a stub interpreter there keeps the passing case fully
# hermetic -- no ``uv`` resolution, no network, no real linter run. It answers the
# ``-c "import yaml"`` probe and echoes its argv for the actual lint invocation.
_STUB_VENV_PYTHON = """#!/usr/bin/env bash
if [ "$1" = "-c" ]; then
  exit 0
fi
echo "STUB_LINTER_INVOKED $*"
exit 0
"""


def _clean_env() -> dict[str, str]:
    return {
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


@pytest.fixture
def isolated_script(tmp_path: Path) -> Path:
    """The real script, copied where NO relative sibling candidate can resolve.

    ``./omnibase_infra``, ``../omnibase_infra`` and ``../../../omnibase_infra`` are
    all relative to the cwd, so the isolated tree is nested deliberately deep with
    no ``omnibase_infra`` anywhere above it.
    """
    workdir = tmp_path / "iso" / "a" / "b" / "c" / "scripts" / "validation"
    workdir.mkdir(parents=True)
    dest = workdir / SCRIPT.name
    shutil.copy2(SCRIPT, dest)
    dest.chmod(0o755)
    return dest


def _cwd_of(script: Path) -> Path:
    """The fake repo root: two levels up from ``scripts/validation/``."""
    return script.parent.parent.parent


def _plant_infra(root: Path) -> Path:
    """A minimal omnibase_infra clone carrying the linter and a stub interpreter."""
    infra = root / "omnibase_infra"
    linter = infra / LINT_REL
    linter.parent.mkdir(parents=True)
    linter.write_text(_STUB_LINTER)
    linter.chmod(0o755)
    venv_python = infra / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text(_STUB_VENV_PYTHON)
    venv_python.chmod(0o755)
    return linter


def _run(script: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)],
        env=env,
        cwd=str(_cwd_of(script)),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


@pytest.mark.unit
def test_script_exists_with_a_bash_shebang() -> None:
    """The hook entry is ``bash scripts/validation/run_topic_lint.sh``.

    The exec bit is deliberately NOT asserted: pre-commit invokes this through
    ``bash``, and the file is mode 644 on the tracked tree.
    """
    assert SCRIPT.is_file(), f"missing script: {SCRIPT}"
    assert SCRIPT.read_text().startswith("#!/usr/bin/env bash"), (
        "script must carry a bash shebang"
    )


@pytest.mark.unit
def test_no_hardcoded_absolute_paths() -> None:
    """Rule #6: the remediation is an ``export`` line, never a machine path."""
    text = SCRIPT.read_text()
    for prefix in ("/" + "Users/", "/" + "Volumes/"):
        assert prefix not in text, f"hardcoded local absolute path in script: {prefix}"


@pytest.mark.unit
def test_both_unset_names_both_variables_and_offers_both_examples(
    isolated_script: Path,
) -> None:
    """UNSET (both) -> exit 2, naming both candidate variables with copyable exports."""
    result = _run(isolated_script, _clean_env())

    assert result.returncode == 2, (
        f"expected exit 2 with both variables unset, got {result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert _BOTH_UNSET_MESSAGE in result.stderr, (
        f"stderr must state neither variable is set; got:\n{result.stderr}"
    )
    assert _UNSET_EXPORT_EXAMPLE in result.stderr, (
        f"stderr must carry the copyable OMNIBASE_INFRA_PATH export example; "
        f"got:\n{result.stderr}"
    )
    assert "export OMNI_HOME=" in result.stderr, (
        f"stderr must also offer OMNI_HOME as an alternative; got:\n{result.stderr}"
    )


@pytest.mark.unit
def test_stale_omnibase_infra_path_names_that_variable_not_omni_home(
    isolated_script: Path, tmp_path: Path
) -> None:
    """STALE OMNIBASE_INFRA_PATH -> names OMNIBASE_INFRA_PATH, never blames OMNI_HOME.

    This is the OMN-17167 correction under test: OMNIBASE_INFRA_PATH is this
    hook's own, higher-priority override. An operator who set it (wrong) must not
    be told "OMNI_HOME is not set" -- that sends them to fix the wrong variable.
    """
    stale_root = tmp_path / "stale_infra_path"
    stale_root.mkdir()
    env = _clean_env()
    env["OMNIBASE_INFRA_PATH"] = str(stale_root)

    result = _run(isolated_script, env)

    expected_missing = str(stale_root / LINT_REL)
    assert result.returncode == 2, (
        f"expected exit 2 on stale OMNIBASE_INFRA_PATH, got {result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert _INFRA_PATH_STALE_MESSAGE in result.stderr, (
        f"stderr must say OMNIBASE_INFRA_PATH is set (not unset); got:\n{result.stderr}"
    )
    assert expected_missing in result.stderr, (
        f"stderr must print the full expanded missing path {expected_missing!r}; "
        f"got:\n{result.stderr}"
    )
    assert _BOTH_UNSET_MESSAGE not in result.stderr, (
        "a stale OMNIBASE_INFRA_PATH must not be reported as unset; "
        f"got:\n{result.stderr}"
    )
    assert "OMNI_HOME is not set" not in result.stderr, (
        "must not blanket-blame OMNI_HOME when OMNIBASE_INFRA_PATH is the "
        f"variable that is actually set-but-wrong; got:\n{result.stderr}"
    )


@pytest.mark.unit
def test_stale_omni_home_when_infra_path_unset_names_omni_home(
    isolated_script: Path, tmp_path: Path
) -> None:
    """STALE OMNI_HOME, OMNIBASE_INFRA_PATH unset -> names OMNI_HOME as the fallback."""
    stale_root = tmp_path / "stale_registry"
    stale_root.mkdir()
    env = _clean_env()
    env["OMNI_HOME"] = str(stale_root)

    result = _run(isolated_script, env)

    expected_missing = str(stale_root / "omnibase_infra" / LINT_REL)
    assert result.returncode == 2, (
        f"expected exit 2 on stale OMNI_HOME, got {result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert _OMNI_HOME_FALLBACK_PREFIX in result.stderr, (
        f"stderr must say OMNIBASE_INFRA_PATH is unset and OMNI_HOME is set; "
        f"got:\n{result.stderr}"
    )
    assert expected_missing in result.stderr, (
        f"stderr must print the full expanded missing path {expected_missing!r}; "
        f"got:\n{result.stderr}"
    )
    assert _BOTH_UNSET_MESSAGE not in result.stderr, (
        "a stale OMNI_HOME must not be reported as unset -- that is the reported "
        f"defect; got:\n{result.stderr}"
    )


@pytest.mark.unit
def test_correct_omni_home_resolves_the_linter(
    isolated_script: Path, tmp_path: Path
) -> None:
    """CORRECT (via OMNI_HOME) -> the preflight stays silent and the linter resolves.

    Pre-OMN-17167 this case FAILED: the script never consulted OMNI_HOME, so a
    correctly-set registry still produced the not-found error. This is the
    OMN-14444 worktree-unrunnable mechanism, closed for this hook.
    """
    registry = tmp_path / "registry"
    registry.mkdir()
    linter = _plant_infra(registry)
    env = _clean_env()
    env["OMNI_HOME"] = str(registry)

    result = _run(isolated_script, env)

    assert _BOTH_UNSET_MESSAGE not in result.stderr
    assert _OMNI_HOME_STALE_MESSAGE not in result.stderr
    assert "STUB_LINTER_INVOKED" in result.stdout, (
        f"the linter under OMNI_HOME must be invoked; got stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert str(linter) in result.stdout or "--scan-contracts" in result.stdout, (
        f"the resolved linter must receive the scan arguments; got:\n{result.stdout}"
    )


@pytest.mark.unit
def test_correct_omnibase_infra_path_resolves_the_linter(
    isolated_script: Path, tmp_path: Path
) -> None:
    """CORRECT (via OMNIBASE_INFRA_PATH) -> resolves directly, no OMNI_HOME needed."""
    infra_root = tmp_path / "infra_only"
    infra_root.mkdir()
    lint_file = infra_root / LINT_REL
    lint_file.parent.mkdir(parents=True)
    lint_file.write_text(_STUB_LINTER)
    lint_file.chmod(0o755)
    venv_python = infra_root / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text(_STUB_VENV_PYTHON)
    venv_python.chmod(0o755)
    env = _clean_env()
    env["OMNIBASE_INFRA_PATH"] = str(infra_root)

    result = _run(isolated_script, env)

    assert _BOTH_UNSET_MESSAGE not in result.stderr
    assert _INFRA_PATH_STALE_MESSAGE not in result.stderr
    assert "STUB_LINTER_INVOKED" in result.stdout, (
        f"the linter under OMNIBASE_INFRA_PATH must be invoked; got stdout:\n"
        f"{result.stdout}\nstderr:\n{result.stderr}"
    )
