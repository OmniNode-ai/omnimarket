# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The clean-install probe runs before the tag step and stops the cut (OMN-26025).

``verify_pypi_pin_resolvability.py`` proves a wheel's pins resolve; it never
imports the package. A wheel whose code imports a module its metadata does not
declare passes it, is tagged, and breaks every clean install. The probe installs
the built wheel into an empty venv and imports it, before the tag exists.
"""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-cut.yml"
PROBE = REPO_ROOT / "src" / "omnimarket" / "handlers" / "release_clean_install_probe.py"
PROBE_NAME_PART = "clean-install probe"
TAG_STEP_ID = "tag"


def _release_steps() -> list[dict[str, Any]]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["release"]["steps"]


def _wheel(directory: Path, *, body: str) -> Path:
    """A wheel for ``probe_dist`` that registers one ``onex.nodes`` entry point."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "probe_dist-1.0.0-py3-none-any.whl"
    files = {
        "probe_dist/__init__.py": "",
        "probe_dist/node.py": body,
        "probe_dist-1.0.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: probe-dist\nVersion: 1.0.0\n",
        "probe_dist-1.0.0.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        "probe_dist-1.0.0.dist-info/entry_points.txt": "[onex.nodes]\nnode_probe = probe_dist.node\n",
    }
    record = []
    for name, text in files.items():
        digest = base64.urlsafe_b64encode(
            hashlib.sha256(text.encode()).digest()
        ).rstrip(b"=")
        record.append(f"{name},sha256={digest.decode()},{len(text.encode())}")
    record.append("probe_dist-1.0.0.dist-info/RECORD,,")
    files["probe_dist-1.0.0.dist-info/RECORD"] = "\n".join(record) + "\n"
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return path


def _probe(dist: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PROBE), str(dist)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_clean_install_probe_passes_a_wheel_that_imports(tmp_path: Path) -> None:
    _wheel(tmp_path / "dist", body="VALUE = 1\n")
    done = _probe(tmp_path / "dist")
    assert done.returncode == 0, done.stdout + done.stderr
    assert "1 entry point(s), 0 failure(s)" in done.stdout


def test_clean_install_probe_fails_a_wheel_missing_a_declared_dependency(
    tmp_path: Path,
) -> None:
    _wheel(tmp_path / "dist", body="import omn26025_not_declared_anywhere\n")
    done = _probe(tmp_path / "dist")
    assert done.returncode == 1, done.stdout + done.stderr
    assert "omn26025_not_declared_anywhere" in done.stderr


def test_clean_install_probe_refuses_a_dist_with_no_single_wheel(
    tmp_path: Path,
) -> None:
    (tmp_path / "dist").mkdir()
    assert _probe(tmp_path / "dist").returncode == 1


def test_clean_install_probe_before_tag_is_ordered_ahead_of_the_tag_step() -> None:
    steps = _release_steps()
    names = [s.get("name", "") for s in steps]
    probe_at = next(i for i, n in enumerate(names) if PROBE_NAME_PART in n.lower())
    tag_at = next(i for i, s in enumerate(steps) if s.get("id") == TAG_STEP_ID)
    build_at = next(i for i, n in enumerate(names) if n == "Build wheel + sdist")
    assert build_at < probe_at < tag_at
    probe = steps[probe_at]
    assert "continue-on-error" not in probe, (
        "a probe that may fail without stopping the job is no gate"
    )
    assert "if" not in probe, "a probe that can be skipped is no gate"
    assert "release_clean_install_probe.py" in probe["run"]


def test_clean_install_probe_before_tag_stops_the_job_before_the_tag_step(
    tmp_path: Path,
) -> None:
    """Run the job's own probe step, then a stand-in for the tag step, as a job would.

    GitHub runs each ``run:`` step under ``bash -e`` and stops the job at the first
    failed step; the stand-in leaves a marker when it is reached.
    """
    steps = _release_steps()
    probe = next(s for s in steps if PROBE_NAME_PART in s.get("name", "").lower())
    work = tmp_path / "job"
    work.mkdir()
    (work / "src").symlink_to(REPO_ROOT / "src")

    def run_job(body: str) -> tuple[int, bool]:
        _wheel(work / "dist", body=body)
        marker = work / "tag-step-reached"
        marker.unlink(missing_ok=True)
        code = 0
        for step_run in (probe["run"], f"touch {marker}"):
            done = subprocess.run(
                ["bash", "-e", "-c", step_run],
                cwd=work,
                env=scrub_git_location_env(os.environ),
                capture_output=True,
                check=False,
            )
            if done.returncode != 0:
                code = done.returncode
                break
        return code, marker.exists()

    code, reached = run_job("import omn26025_not_declared_anywhere\n")
    assert (code, reached) == (1, False), (
        "a wheel missing a dependency must not reach the tag step"
    )
    code, reached = run_job("VALUE = 1\n")
    assert (code, reached) == (0, True), (
        "positive control: a sound wheel reaches the tag step"
    )
