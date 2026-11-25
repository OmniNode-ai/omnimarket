# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-16084: the CI Summary poll waits on its jobs; the cap is a declared setting.

The poll step in ``ci.yml`` is run here as the shell script it is, against stub
``gh``, ``jq`` and gate executables, so the loop's own exits are exercised:
PENDING keeps polling while the deadline is open, FAILURE exits at once, and a
PENDING that is still PENDING at the cap fails closed.

Measured on omnimarket#6749 (CI run created 2026-11-24T19:10:09Z, updated at
its last job 19:55:04Z): the 20 test splits took 8.4 to 39.0 minutes, lint 29.3,
composed-runtime-boot 29.6, and the run took 45 minutes from creation to its last
job. The cap must exceed that with margin.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
POLL_STEP = "Poll run jobs and compute fail-closed CI Summary verdict"
SLOWEST_OBSERVED_MINUTES = 45  # 19:10:09Z creation to 19:55:04Z last job, #6749
REQUIRED_MARGIN = 2


def _job() -> dict:
    return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]["ci-summary"]


def _poll_step() -> dict:
    return next(s for s in _job()["steps"] if s.get("name") == POLL_STEP)


def test_cap_is_a_declared_setting_above_the_slowest_measured_run() -> None:
    step = _poll_step()
    cap = int(step["env"]["DEADLINE_MINUTES"])
    assert cap >= SLOWEST_OBSERVED_MINUTES * REQUIRED_MARGIN
    # The job's own timeout is a backstop above the poll cap, never below it.
    assert _job()["timeout-minutes"] > cap
    assert "${DEADLINE_MINUTES}" in step["run"] or "DEADLINE_MINUTES *" in step["run"]


def test_cap_value_is_not_hard_coded_in_the_script() -> None:
    cap = str(_poll_step()["env"]["DEADLINE_MINUTES"])
    script = _poll_step()["run"]
    for line in script.splitlines():
        if line.lstrip().startswith("#"):
            continue
        assert f"* {cap} " not in line + " ", line
        assert f"{cap}m" not in line, line


def _run_poll(
    tmp_path: Path, gate_codes: list[int], *, cap_minutes: int
) -> subprocess.CompletedProcess[str]:
    """Run the real poll step with a stub gate that returns ``gate_codes`` in order."""

    script = _poll_step()["run"].replace("${{ github.event_name }}", "push")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "gh").write_text("#!/bin/sh\necho '[]'\n", encoding="utf-8")
    # jq -s '.' reads the stream and prints a list; -e is only reached on pull_request.
    (bin_dir / "jq").write_text("#!/bin/sh\ncat >/dev/null\necho '[]'\n")
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    gate_dir = tmp_path / "scripts" / "ci"
    gate_dir.mkdir(parents=True)
    counter = tmp_path / "calls"
    (gate_dir / "ci_summary_gate.py").write_text(
        "import sys, pathlib\n"
        f"codes = [{', '.join(str(c) for c in gate_codes)}]\n"
        f"p = pathlib.Path({str(counter)!r})\n"
        "n = int(p.read_text()) if p.exists() else 0\n"
        "if '--report-only' not in sys.argv:\n"
        "    p.write_text(str(n + 1))\n"
        "print('stub verdict', n)\n"
        "raise SystemExit(0 if '--report-only' in sys.argv else codes[min(n, len(codes) - 1)])\n",
        encoding="utf-8",
    )
    (bin_dir / "gh").chmod(0o755)
    (bin_dir / "jq").chmod(0o755)
    (bin_dir / "sleep").chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        **{k: str(v) for k, v in _poll_step()["env"].items() if "${{" not in str(v)},
        "DEADLINE_MINUTES": str(cap_minutes),
        "POLL_INTERVAL_SECONDS": "0",
        # The step's `${{ }}` env values, which GitHub fills and a test must supply.
        **dict.fromkeys(("RUN_ID", "RUN_ATTEMPT", "PR_NUMBER"), "1"),
        **dict.fromkeys(("GH_TOKEN", "HEAD_SHA", "ACTOR", "REF_NAME"), "x"),
        "GH_REPO": "o/r",
    }
    return subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def test_pending_keeps_polling_until_the_jobs_finish_green(tmp_path: Path) -> None:
    """Jobs still running (PENDING) at poll after poll, then green: exit 0."""

    done = _run_poll(tmp_path, [2, 2, 2, 2, 0], cap_minutes=90)
    assert done.returncode == 0, done.stdout + done.stderr
    assert (tmp_path / "calls").read_text() == "5"
    assert "CI Summary = SUCCESS" in done.stdout


def test_finished_red_fails_at_once_with_the_cap_still_open(tmp_path: Path) -> None:
    done = _run_poll(tmp_path, [2, 1, 0], cap_minutes=90)
    assert done.returncode == 1, done.stdout + done.stderr
    assert (tmp_path / "calls").read_text() == "2"
    assert "CI Summary = FAILURE" in done.stdout + done.stderr


def test_a_job_that_never_starts_fails_closed_at_the_cap(tmp_path: Path) -> None:
    """PENDING with the cap spent is a failure, never a pass and never a hang."""

    done = _run_poll(tmp_path, [2], cap_minutes=0)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "poll deadline (0m) reached with gating jobs still pending" in (
        done.stdout + done.stderr
    )


def test_control_the_same_stub_passes_when_the_gate_is_green(tmp_path: Path) -> None:
    """Positive control: the harness reports a pass when the gate says pass."""

    done = _run_poll(tmp_path, [0], cap_minutes=0)
    assert done.returncode == 0, done.stdout + done.stderr
