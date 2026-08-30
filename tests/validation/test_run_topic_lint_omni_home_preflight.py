# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMNI_HOME fail-fast preflight for the topic-naming-lint hook (OMN-17167).

``scripts/validation/run_topic_lint.sh`` runs the canonical linter, which lives in
``omnibase_infra``. Reported by a contractor 2026-08-30: with a stale or unset
``OMNI_HOME`` the hook printed nothing but a not-found path -- it never read
OMNI_HOME at all, so from a worktree at
``$OMNI_HOME/omni_worktrees/<ticket>/omnimarket`` it was unrunnable and named no
variable the operator could act on (the OMN-14444 mechanism).

Doctrine under test: omni_home CLAUDE.md rule 8 (fail fast on missing env, never a
silent default), rule 6 (no absolute paths -- the remediation is an ``export``
line), and memory ``feedback_own_errors_give_full_paths`` (name the variable AND
the full missing path).

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
_UNSET_MESSAGE = "OMNI_HOME is not set."
_SIBLING_LAYOUT = (
    "It must be the directory containing the sibling clones (omnibase_infra)."
)
_EXPORT_EXAMPLE = "export OMNI_HOME=$HOME/omninode"
_STALE_MESSAGE = "OMNI_HOME is set to"

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
def test_unset_omni_home_names_the_variable_and_the_expected_layout(
    isolated_script: Path,
) -> None:
    """UNSET -> exit 2, naming the variable, the layout, and a copyable export."""
    result = _run(isolated_script, _clean_env())

    assert result.returncode == 2, (
        f"expected exit 2 on unset OMNI_HOME, got {result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert _UNSET_MESSAGE in result.stderr, (
        f"stderr must name the unset variable; got:\n{result.stderr}"
    )
    assert _SIBLING_LAYOUT in result.stderr, (
        f"stderr must state what OMNI_HOME points at; got:\n{result.stderr}"
    )
    assert _EXPORT_EXAMPLE in result.stderr, (
        f"stderr must carry the copyable export example; got:\n{result.stderr}"
    )


@pytest.mark.unit
def test_stale_omni_home_prints_the_full_missing_path(
    isolated_script: Path, tmp_path: Path
) -> None:
    """STALE -> exit 2 naming the FULL expanded missing path AND the variable."""
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
    assert expected_missing in result.stderr, (
        f"stderr must print the full expanded missing path {expected_missing!r}; "
        f"got:\n{result.stderr}"
    )
    assert _STALE_MESSAGE in result.stderr, (
        f"stderr must say OMNI_HOME is set (not unset); got:\n{result.stderr}"
    )
    assert _UNSET_MESSAGE not in result.stderr, (
        "a stale OMNI_HOME must not be reported as unset -- that is the reported "
        f"defect; got:\n{result.stderr}"
    )


@pytest.mark.unit
def test_correct_omni_home_resolves_the_linter(
    isolated_script: Path, tmp_path: Path
) -> None:
    """CORRECT -> the preflight stays silent and the linter resolves under OMNI_HOME.

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

    assert _UNSET_MESSAGE not in result.stderr
    assert _STALE_MESSAGE not in result.stderr
    assert "STUB_LINTER_INVOKED" in result.stdout, (
        f"the linter under OMNI_HOME must be invoked; got stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert str(linter) in result.stdout or "--scan-contracts" in result.stdout, (
        f"the resolved linter must receive the scan arguments; got:\n{result.stdout}"
    )
