# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# test-literal-ok: asserts wheel paths do not contain local absolute paths
"""Verify the hatchling wheel excludes non-production content.

Ensures experiments/, .onex_state/, work-tracking YAML, and machine-specific
absolute paths never ship in the published wheel.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

_WHEEL_PATH: Path | None = None


def _build_wheel() -> Path:
    """Build the wheel once per test session and return its path."""
    global _WHEEL_PATH
    if _WHEEL_PATH is not None and _WHEEL_PATH.exists():
        return _WHEEL_PATH

    dist_dir = Path(tempfile.mkdtemp(prefix="omnimarket_wheel_test_"))
    result = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(dist_dir)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"uv build --wheel failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    wheels = list(dist_dir.glob("*.whl"))
    if not wheels:
        raise RuntimeError(f"No wheel produced in {dist_dir}. stdout={result.stdout}")
    _WHEEL_PATH = wheels[0]
    return _WHEEL_PATH


def _wheel_paths() -> list[str]:
    wheel = _build_wheel()
    with zipfile.ZipFile(wheel) as zf:
        return zf.namelist()


@pytest.mark.unit
def test_wheel_excludes_experiments_directory() -> None:
    # Skip if experiments/ is still present in src/ (prerequisite: OMN-10570 / PR #541)
    if (REPO_ROOT / "src" / "omnimarket" / "experiments").exists():
        pytest.skip(
            "experiments/ still present in src/omnimarket/ — "
            "merge OMN-10570 (PR #541) first"
        )
    paths = _wheel_paths()
    violations = [p for p in paths if re.search(r"(^|/)experiments/", p)]
    assert violations == [], f"wheel contains experiments/ paths: {violations}"


@pytest.mark.unit
def test_wheel_excludes_onex_state_directory() -> None:
    paths = _wheel_paths()
    violations = [p for p in paths if re.search(r"(^|/)\.onex_state/", p)]
    assert violations == [], f"wheel contains .onex_state/ paths: {violations}"


@pytest.mark.unit
def test_wheel_excludes_work_tracking_yamls() -> None:
    """No OMN-*.yaml work-tracking files should be in the wheel."""
    paths = _wheel_paths()
    violations = [p for p in paths if re.search(r"OMN-\d+\.yaml$", p)]
    assert violations == [], f"wheel contains work-tracking YAML files: {violations}"


@pytest.mark.unit
def test_wheel_excludes_machine_specific_paths() -> None:
    """No wheel entry should contain /Users/ or /Volumes/ in its filename."""
    paths = _wheel_paths()
    violations = [
        p
        for p in paths
        if re.search(r"/Users/[A-Za-z0-9._-]+", p)
        or re.search(r"/Volumes/[A-Za-z0-9._-]+", p)
    ]
    assert violations == [], (
        f"wheel contains machine-specific absolute paths: {violations}"
    )


@pytest.mark.unit
def test_wheel_contains_omnimarket_package() -> None:
    """Sanity check: the wheel must contain at least one omnimarket/ source file."""
    paths = _wheel_paths()
    omnimarket_files = [p for p in paths if p.startswith("omnimarket/")]
    assert len(omnimarket_files) > 0, (
        "wheel contains no omnimarket/ package files — build may be broken"
    )
