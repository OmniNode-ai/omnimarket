# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Sibling-resolution fail-fast preflight for subscriber-dispatcher-resolution
(OMN-17167).

``scripts/ci/check_subscriber_dispatcher_resolution.sh`` runs the canonical
validator, which lives in ``omnibase_infra`` (OMN-16939 deliberately keeps one
implementation in one repo). Reported by a contractor 2026-08-30: with a stale or
unset sibling-resolution variable the failure listed its five candidates as
LITERAL unexpanded text (``$OMNI_HOME/omnibase_infra/src``), so the operator
never saw the path that was actually probed and a stale variable was
byte-indistinguishable from an unset one.

OMN-17167 correction (2026-08-30): the first cut of this fix routed every
failure through a message that unconditionally named ``OMNI_HOME``, even though
``OMNIBASE_INFRA_PATH`` is this gate's own, higher-priority override -- checked
before ``$OMNI_HOME/omnibase_infra/src`` in the script's candidate list -- and
``OMNI_HOME`` here is only a worktree-friendly fallback. Blaming ``OMNI_HOME``
when the operator already set (a wrong) ``OMNIBASE_INFRA_PATH`` sends them to fix
the variable that ISN'T the problem. These tests assert the preflight names
whichever candidate variable was actually set-but-wrong, and only talks about
``OMNI_HOME`` as a fallback option when ``OMNIBASE_INFRA_PATH`` was never set.

Doctrine under test: omni_home CLAUDE.md rule 8 (fail fast on missing env, never a
silent default), rule 6 (no absolute paths -- the remediation is an ``export``
line), and memory ``feedback_own_errors_give_full_paths`` (name the actual
variable AND the full missing path).

What the gate VALIDATES is untouched: the CI checkout candidate
(``./omnibase_infra/src``, used by .github/workflows/subscriber-dispatcher-resolution.yml)
still wins ahead of every other candidate, so CI never reaches the preflight.
These tests drive THE real script end-to-end via subprocess from an isolated tmp
cwd; git env vars are stripped from the child per the OMN-14746/14744
worktree-safety lesson.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ci" / "check_subscriber_dispatcher_resolution.sh"

VALIDATOR_REL = (
    Path("omnibase_infra") / "validators" / "subscriber_dispatcher_resolution.py"
)

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

_STUB_UV = """#!/usr/bin/env bash
echo "STUB_UV_INVOKED $*"
exit 0
"""


def _clean_env(stub_bin: Path) -> dict[str, str]:
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
    env["PATH"] = f"{stub_bin}{os.pathsep}{env.get('PATH', '')}"
    return env


@pytest.fixture
def stub_bin(tmp_path: Path) -> Path:
    """A PATH entry whose ``uv`` records its argv instead of running the validator."""
    bin_dir = tmp_path / "stub_bin"
    bin_dir.mkdir()
    stub = bin_dir / "uv"
    stub.write_text(_STUB_UV)
    stub.chmod(0o755)
    return bin_dir


@pytest.fixture
def isolated_script(tmp_path: Path) -> Path:
    """The real script, copied where NO relative sibling candidate can resolve.

    ``./omnibase_infra/src``, ``../omnibase_infra/src`` and
    ``../../../omnibase_infra/src`` are all relative to the cwd, so the isolated
    tree is nested deliberately deep with no ``omnibase_infra`` anywhere above it.
    """
    workdir = tmp_path / "iso" / "a" / "b" / "c" / "scripts" / "ci"
    workdir.mkdir(parents=True)
    dest = workdir / SCRIPT.name
    shutil.copy2(SCRIPT, dest)
    dest.chmod(0o755)
    return dest


def _cwd_of(script: Path) -> Path:
    """The fake repo root: two levels up from ``scripts/ci/``."""
    return script.parent.parent.parent


def _plant_infra(root: Path) -> Path:
    """A minimal omnibase_infra clone carrying the validator, as a stub."""
    validator = root / "omnibase_infra" / "src" / VALIDATOR_REL
    validator.parent.mkdir(parents=True)
    validator.write_text("# stub validator\n")
    return validator


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
def test_script_exists_and_is_executable() -> None:
    assert SCRIPT.is_file(), f"missing script: {SCRIPT}"
    assert os.access(SCRIPT, os.X_OK), "script must be executable"


@pytest.mark.unit
def test_no_hardcoded_absolute_paths() -> None:
    """Rule #6: the remediation is an ``export`` line, never a machine path."""
    text = SCRIPT.read_text()
    for prefix in ("/" + "Users/", "/" + "Volumes/"):
        assert prefix not in text, f"hardcoded local absolute path in script: {prefix}"


@pytest.mark.unit
def test_both_unset_names_both_variables_and_offers_both_examples(
    isolated_script: Path, stub_bin: Path
) -> None:
    """UNSET (both) -> exit 2, naming both candidate variables with copyable exports."""
    result = _run(isolated_script, _clean_env(stub_bin))

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
    assert "STUB_UV_INVOKED" not in result.stdout, (
        "the gate must not run when its validator cannot be resolved"
    )


@pytest.mark.unit
def test_stale_omnibase_infra_path_names_that_variable_not_omni_home(
    isolated_script: Path, stub_bin: Path, tmp_path: Path
) -> None:
    """STALE OMNIBASE_INFRA_PATH -> names OMNIBASE_INFRA_PATH, never blames OMNI_HOME.

    This is the OMN-17167 correction under test: OMNIBASE_INFRA_PATH is this
    gate's own, higher-priority override. An operator who set it (wrong) must not
    be told "OMNI_HOME is not set" -- that sends them to fix the wrong variable.
    """
    stale_root = tmp_path / "stale_infra_path"
    stale_root.mkdir()
    env = _clean_env(stub_bin)
    env["OMNIBASE_INFRA_PATH"] = str(stale_root)

    result = _run(isolated_script, env)

    expected_missing = str(stale_root / "src" / VALIDATOR_REL)
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
    assert "OMNI_HOME is not set" not in result.stderr, (
        "must not blanket-blame OMNI_HOME when OMNIBASE_INFRA_PATH is the "
        f"variable that is actually set-but-wrong; got:\n{result.stderr}"
    )


@pytest.mark.unit
def test_stale_omni_home_when_infra_path_unset_names_omni_home(
    isolated_script: Path, stub_bin: Path, tmp_path: Path
) -> None:
    """STALE OMNI_HOME, OMNIBASE_INFRA_PATH unset -> names OMNI_HOME as the fallback.

    This is the case the pre-OMN-17167 script could not express: it printed the
    candidate list as literal ``$OMNI_HOME/...`` text, never the expanded path.
    """
    stale_root = tmp_path / "stale_registry"
    stale_root.mkdir()
    env = _clean_env(stub_bin)
    env["OMNI_HOME"] = str(stale_root)

    result = _run(isolated_script, env)

    expected_missing = str(stale_root / "omnibase_infra" / "src" / VALIDATOR_REL)
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
def test_correct_omni_home_resolves_and_does_not_preflight_fail(
    isolated_script: Path, stub_bin: Path, tmp_path: Path
) -> None:
    """CORRECT -> the preflight stays silent and the validator resolves."""
    registry = tmp_path / "registry"
    registry.mkdir()
    _plant_infra(registry)
    env = _clean_env(stub_bin)
    env["OMNI_HOME"] = str(registry)

    result = _run(isolated_script, env)

    assert result.returncode == 0, (
        f"expected the gate to run, got {result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert _BOTH_UNSET_MESSAGE not in result.stderr
    assert _OMNI_HOME_STALE_MESSAGE not in result.stderr
    assert "STUB_UV_INVOKED" in result.stdout, (
        f"the validator must actually be invoked; got stdout:\n{result.stdout}"
    )


@pytest.mark.unit
def test_ci_checkout_candidate_still_wins_over_omni_home(
    isolated_script: Path, stub_bin: Path, tmp_path: Path
) -> None:
    """``./omnibase_infra/src`` (the CI checkout) still resolves first.

    Regression guard on the OMN-17167 change: the preflight must not disturb the
    resolution order .github/workflows/subscriber-dispatcher-resolution.yml relies
    on, where neither OMNIBASE_INFRA_PATH nor OMNI_HOME is ever set.
    """
    _plant_infra(_cwd_of(isolated_script))

    result = _run(isolated_script, _clean_env(stub_bin))

    assert result.returncode == 0, (
        f"CI checkout must resolve with both variables unset, got {result.returncode}\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert _BOTH_UNSET_MESSAGE not in result.stderr, (
        "the preflight must not fire when the CI checkout resolves; "
        f"got:\n{result.stderr}"
    )
    assert "./omnibase_infra/src" in result.stderr, (
        f"the CI checkout candidate must be the one used; got:\n{result.stderr}"
    )
