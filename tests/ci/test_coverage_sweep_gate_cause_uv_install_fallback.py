"""OMN-23132: the Coverage Sweep Gate census must survive a failed uv release download.

Recorded failure (2026-11-08, omnimarket pull requests #5150..#5353): the
``astral-sh/setup-uv`` step downloads ``uv-x86_64-unknown-linux-gnu.tar.gz`` from
github.com release assets. On 09:12Z-10:30Z that download answered
``Unexpected HTTP response: 504`` three times in a row (job log of Coverage Sweep
Gate on #5253: "Downloading uv from https://github.com/astral-sh/uv/releases/download/
0.13.7/... Unexpected HTTP response: 504", ``##[error]`` after the third wait). The
same step failed in Detect Changes (every shard skipped, the gate refused with
``detect-changes=failure split_count=''``) and in Tests shards (the gate then died
with ``ARTIFACT_MISSING split N ... .coverage.N is absent``).

The gate stays fail-closed. What these tests pin is the one thing the three jobs on
the census path lacked: a second origin (PyPI) for uv when the release download fails.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
SETUP_UV_PREFIX = "astral-sh/setup-uv@"
CENSUS_PATH_JOBS = ("detect-changes", "test", "coverage-sweep-gate")


def _jobs() -> dict[str, Any]:
    return yaml.safe_load(CI_WORKFLOW.read_text())["jobs"]


def _steps(job: str) -> list[dict[str, Any]]:
    return list(_jobs()[job]["steps"])


def _setup_uv_index(steps: list[dict[str, Any]]) -> int:
    hits = [
        i
        for i, s in enumerate(steps)
        if str(s.get("uses", "")).startswith(SETUP_UV_PREFIX)
    ]
    assert len(hits) == 1, f"expected exactly one setup-uv step, found {len(hits)}"
    return hits[0]


@pytest.mark.parametrize("job", CENSUS_PATH_JOBS)
def test_coverage_sweep_gate_cause_uv_install_has_pypi_fallback(job: str) -> None:
    steps = _steps(job)
    i = _setup_uv_index(steps)
    install = steps[i]
    assert install.get("continue-on-error") is True, (
        f"{job}: a 504 on the uv release download ends the job; the step must hand "
        "the failure to the PyPI fallback"
    )
    step_id = install.get("id")
    assert step_id, f"{job}: the setup-uv step needs an id for the fallback's condition"
    fallback = steps[i + 1]
    assert fallback.get("if") == f"steps.{step_id}.outcome == 'failure'", (
        f"{job}: the step after setup-uv must run only when setup-uv failed"
    )
    script = fallback["run"]
    assert "pip install" in script
    assert " uv" in script
    assert "GITHUB_PATH" in script, (
        f"{job}: the fallback must put uv on PATH for later steps"
    )
    assert 'uv" --version' in script, f"{job}: the fallback must prove uv runs"


def test_coverage_sweep_gate_cause_gate_still_refuses_without_shards() -> None:
    """The fallback must not turn the census into a pass: the refusal stays."""
    steps = _steps("coverage-sweep-gate")
    refusal = next(
        s
        for s in steps
        if "Refuse when the test shards did not run" in s.get("name", "")
    )
    assert "exit 1" in refusal["run"]
    combine = next(s for s in steps if "Combine shard coverage" in s.get("name", ""))
    assert not combine.get("continue-on-error")
    assert "aggregate_coverage_artifacts.py" in combine["run"]


def test_coverage_sweep_gate_cause_fallback_script_installs_uv_and_exports_path(
    tmp_path: Path,
) -> None:
    """Run the fallback's own shell against a recording python3 shim (no network)."""
    steps = _steps("coverage-sweep-gate")
    script = steps[_setup_uv_index(steps) + 1]["run"]

    bin_dir = tmp_path / "bin"
    user_base = tmp_path / "userbase"
    (user_base / "bin").mkdir(parents=True)
    calls = tmp_path / "pip_calls.txt"
    shim = bin_dir / "python3"
    bin_dir.mkdir()
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1 $2" = "-m site" ]; then echo "$FAKE_USER_BASE"; exit 0; fi\n'
        'if [ "$1 $2 $3" = "-m pip install" ]; then echo "$@" >> "$FAKE_PIP_CALLS"; exit 0; fi\n'
        'echo "unexpected python3 call: $@" >&2; exit 9\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR)
    uv = user_base / "bin" / "uv"
    uv.write_text('#!/usr/bin/env bash\necho "uv 0.0.0-test"\n')
    uv.chmod(uv.stat().st_mode | stat.S_IXUSR)
    github_path = tmp_path / "github_path.txt"

    result = subprocess.run(
        ["bash", "-e", "-c", script],
        env={
            "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "FAKE_USER_BASE": str(user_base),
            "FAKE_PIP_CALLS": str(calls),
            "GITHUB_PATH": str(github_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "uv 0.0.0-test" in result.stdout
    assert str(user_base / "bin") in github_path.read_text()
    recorded = calls.read_text()
    assert recorded.strip().endswith("uv"), recorded
