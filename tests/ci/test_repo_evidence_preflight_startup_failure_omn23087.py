# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""A Repo Evidence Gate run that ended startup_failure refuses the preflight at once.

The gate's dod-verify job posts only after the run starts. A run that ends
startup_failure has no jobs, so no dod-verify check ever exists and the
preflight used to poll its whole window, then report a timeout (and, on the way,
a rate limit it had helped cause). The preflight now reads the gate's own run
for the head and refuses at once, naming the startup failure. Every other state
keeps the old behavior: wait, then time out.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
LEGACY_KEYED = (
    "auto-merge.yml",
    "dep-health-gate.yml",
    "plugin-compat-gate.yml",
)
POLLERS = (
    "auto-merge.yml",
    "ci.yml",
    "dep-health-gate.yml",
    "plugin-compat-gate.yml",
    "validator-runtime-profiles.yml",
)
STUB = """
sleep() { return 0; }
gh() {
    case "$2" in
        *"check_name=repo-evidence%20%2F%20dod-verify"*)
            jq -r "$4" "$CHECKS_FIXTURE" ;;
        *"actions/workflows/call-repo-evidence-gate.yml/runs?head_sha=${HEAD_SHA}&event=pull_request_target"*)
            if [ "$GATE_READ" = "fail" ]; then
                echo "HTTP 502" >&2
                return 1
            fi
            jq -r "$4" "$GATE_FIXTURE" ;;
        *)
            echo "unexpected API path" >&2
            return 1 ;;
    esac
}
"""


def _script(filename: str) -> str:
    workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
    key = "occ-preflight" if filename in LEGACY_KEYED else "repo-evidence-preflight"
    script = workflow["jobs"][key]["steps"][0]["run"]
    assert "deadline=2580" in script
    # Three iterations of the poll with the clock mocked away, as the sibling test does.
    return script.replace("deadline=2580", "deadline=30")


def _run(
    filename: str,
    tmp_path: Path,
    *,
    checks: list[dict],
    gate_runs: list[dict],
    gate_read: str = "ok",
) -> subprocess.CompletedProcess[str]:
    (tmp_path / "checks.json").write_text(json.dumps({"check_runs": checks}))
    (tmp_path / "gate.json").write_text(json.dumps({"workflow_runs": gate_runs}))
    return subprocess.run(
        [
            "bash",
            "-e",
            "-c",
            STUB
            + _script(filename).replace("${{ github.event_name }}", "pull_request"),
        ],
        env={
            **os.environ,
            "CHECKS_FIXTURE": str(tmp_path / "checks.json"),
            "GATE_FIXTURE": str(tmp_path / "gate.json"),
            "GATE_READ": gate_read,
            "REPO": "OmniNode-ai/omnimarket",
            "HEAD_SHA": "a" * 40,
            "GITHUB_EVENT_NAME": "pull_request",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )


def _gate(run_id: int, created: str, status: str, conclusion: str | None) -> dict:
    return {
        "id": run_id,
        "created_at": created,
        "status": status,
        "conclusion": conclusion,
    }


STARTUP_FAILED = _gate(
    42652283166, "2026-11-09T16:11:49Z", "completed", "startup_failure"
)


@pytest.mark.parametrize("filename", POLLERS)
def test_startup_failure_of_the_gate_refuses_at_once(
    filename: str, tmp_path: Path
) -> None:
    result = _run(filename, tmp_path, checks=[], gate_runs=[STARTUP_FAILED])
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "ended startup_failure" in output
    assert "42652283166" in output
    assert "Timed out" not in output
    # It refuses on the first poll, not after the window.
    assert output.count("Waiting for repo-evidence") == 1


@pytest.mark.parametrize("filename", POLLERS)
@pytest.mark.parametrize(
    "gate_runs",
    [
        [],
        [_gate(1, "2026-11-09T16:11:49Z", "in_progress", None)],
        [_gate(1, "2026-11-09T16:11:49Z", "queued", None)],
        [_gate(1, "2026-11-09T16:11:49Z", "completed", "failure")],
        [_gate(1, "2026-11-09T16:11:49Z", "completed", "cancelled")],
        # A newer run is replacing the one that failed to start: keep waiting.
        [
            STARTUP_FAILED,
            _gate(2, "2026-11-09T16:20:00Z", "in_progress", None),
        ],
    ],
    ids=[
        "no-run",
        "in-progress",
        "queued",
        "failure",
        "cancelled",
        "newer-run-replaces",
    ],
)
def test_any_other_gate_state_keeps_waiting_then_times_out(
    filename: str, gate_runs: list[dict], tmp_path: Path
) -> None:
    result = _run(filename, tmp_path, checks=[], gate_runs=gate_runs)
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "Timed out waiting for repo-evidence / dod-verify" in output
    assert "ended startup_failure" not in output


@pytest.mark.parametrize("filename", POLLERS)
def test_unreadable_gate_run_keeps_waiting_then_times_out(
    filename: str, tmp_path: Path
) -> None:
    result = _run(
        filename, tmp_path, checks=[], gate_runs=[STARTUP_FAILED], gate_read="fail"
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "Timed out waiting for repo-evidence / dod-verify" in output
    assert "ended startup_failure" not in output
    assert "Repo Evidence Gate run read failed; still waiting" in output


@pytest.mark.parametrize("filename", POLLERS)
def test_a_posted_verdict_is_read_before_the_gate_run(
    filename: str, tmp_path: Path
) -> None:
    posted = [
        {"id": 7, "app": {"id": 15368}, "status": "completed", "conclusion": "success"}
    ]
    result = _run(filename, tmp_path, checks=posted, gate_runs=[STARTUP_FAILED])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Repo evidence passed" in result.stdout
