# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Exercise repo evidence admission without OCC contexts; queue evidence support remains pending."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.ci import ci_summary_gate as gate

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
CONTEXT = "repo-evidence / dod-verify"
JOB_KEY = "repo-evidence-preflight"
# OMN-20074: these four workflows still key the same wait job occ-preflight.
# omniclaude's advisory-job gate grandfathers each by "<file>::occ-preflight" in
# its census baseline (none of the four files carries a required context), so
# the key moves only after that baseline entry is renamed in omniclaude.
LEGACY_KEY = "occ-preflight"
LEGACY_KEYED = (
    "auto-merge.yml",
    "dep-health-gate.yml",
    "market-skill-baseline.yml",
    "plugin-compat-gate.yml",
)
POLLERS = (
    "auto-merge.yml",
    "ci.yml",
    "dep-health-gate.yml",
    "market-skill-baseline.yml",
    "plugin-compat-gate.yml",
    "validator-runtime-profiles.yml",
)


def _poller(filename: str) -> dict:
    workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
    return workflow["jobs"][LEGACY_KEY if filename in LEGACY_KEYED else JOB_KEY]


@pytest.mark.parametrize("filename", POLLERS)
@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        ([(1, 15368, "completed", "success")], 0),
        ([(1, 15368, "completed", "failure")], 1),
        ([(1, 15368, "completed", "skipped")], 1),
        ([(1, 15368, "completed", "cancelled")], 1),
        ([], 1),
        ([(1, 99999, "completed", "success")], 1),
        (
            [(1, 15368, "completed", "success"), (2, 15368, "completed", "failure")],
            1,
        ),
        ([(1, 15368, "completed", "success"), (2, 15368, "queued", None)], 1),
        (
            [(1, 15368, "completed", "success"), (2, 99999, "completed", "failure")],
            0,
        ),
    ],
)
def test_repo_evidence_poller_admission(
    filename: str, checks: list[tuple], expected: int, tmp_path: Path
) -> None:
    """Run the actual YAML shell with API fixtures and an instantaneous clock."""
    poller = _poller(filename)
    assert poller["name"] == "Repo Evidence Dependency"
    script = poller["steps"][0]["run"]
    assert "check_name=repo-evidence%20%2F%20dod-verify" in script
    assert "check_name=occ-preflight" not in script
    # The poller's real deadline is 2580 seconds. With the clock mocked away, the
    # loop would still run 258 iterations on every absent or pending verdict, and
    # that took the module past the 30 second per-check budget of the evidence
    # verifier. Assert the shipped deadline, then shrink it to three iterations.
    assert "deadline=2580" in script
    script = script.replace("deadline=2580", "deadline=30")
    fixture = tmp_path / "check-runs.json"
    fixture.write_text(
        json.dumps(
            {
                "check_runs": [
                    {
                        "id": identifier,
                        "app": {"id": app},
                        "status": status,
                        "conclusion": conclusion,
                    }
                    for identifier, app, status, conclusion in checks
                ]
            }
        )
    )
    # The shell still runs the workflow's jq selector. Only network and waiting
    # are replaced, and an unexpected API path fails the step.
    prefix = """
sleep() { return 0; }
gh() {
    if [[ "$2" != *"check_name=repo-evidence%20%2F%20dod-verify"* ]]; then
        echo "unexpected API path" >&2
        return 1
    fi
    jq -r "$4" "$GH_FIXTURE"
}
"""
    result = subprocess.run(
        [
            "bash",
            "-e",
            "-c",
            prefix + script.replace("${{ github.event_name }}", "pull_request"),
        ],
        env={
            **os.environ,
            "GH_FIXTURE": str(fixture),
            "REPO": "OmniNode-ai/omnimarket",
            "HEAD_SHA": "a" * 40,
            "GITHUB_EVENT_NAME": "pull_request",
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == expected, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("status", "conclusion", "expected"),
    [
        ("completed", "success", gate.EXIT_SUCCESS),
        ("completed", "failure", gate.EXIT_FAILURE),
        ("completed", "skipped", gate.EXIT_FAILURE),
        ("completed", "cancelled", gate.EXIT_FAILURE),
        ("queued", None, gate.EXIT_PENDING),
    ],
)
def test_repo_evidence_summary_verdict(
    status: str, conclusion: str | None, expected: int
) -> None:
    assert CONTEXT in gate.EXPECTED_EXTERNAL_CONTEXTS
    rows = [
        {"name": name, "status": "completed", "conclusion": "success"}
        for name in gate.EXPECTED_EXTERNAL_CONTEXTS
    ]
    target = next(row for row in rows if row["name"] == CONTEXT)
    target.update(status=status, conclusion=conclusion)
    code, report = gate.evaluate_external(rows)
    assert code == expected, report
    rows.remove(target)
    code, report = gate.evaluate_external(rows)
    assert code == gate.EXIT_PENDING, report


def test_repo_evidence_summary_queue_expects_no_occ_contexts() -> None:
    assert CONTEXT in gate.EXPECTED_EXTERNAL_CONTEXTS
    assert CONTEXT in gate.expected_external_contexts("pull_request")
    assert CONTEXT in gate.expected_external_contexts(None)
    assert CONTEXT in gate.expected_external_contexts("unknown")
    queue = gate.expected_external_contexts("merge_group")
    assert CONTEXT not in queue
    assert set(queue) == set(gate.EXPECTED_EXTERNAL_CONTEXTS) - {CONTEXT}
    assert "occ-preflight / eligibility" not in queue
    assert "OCC Emitter Golden Gate" in queue
    assert "ONEX Change Control Schema Compatibility" in queue
    assert "verify / verify" not in queue
    assert "call-reject-skip-token / occ-preflight / eligibility" not in queue
    assert "call-reject-skip-token / scan / reject-skip-gate-token" in queue
    assert "OCC Companion Merged Gate (OMN-15214)" not in gate.STRICT_GATE_JOBS
    assert "Repo Evidence Dependency" in gate.STRICT_GATE_JOBS


def _needs(job: dict) -> list[str]:
    needs = job.get("needs") or []
    return [needs] if isinstance(needs, str) else list(needs)


def test_repo_evidence_wait_is_keyed_by_what_it_waits_for() -> None:
    """OMN-20074: the wait job reads repo evidence, so its key no longer names OCC.

    Every workflow that waits on ``repo-evidence / dod-verify`` declares the job
    as ``repo-evidence-preflight``, except the four in ``LEGACY_KEYED``, and no
    other workflow declares, needs or reads the result of a job keyed
    ``occ-preflight``. The key keeps the word ``preflight``: omnibase_core's
    shape-gate independence guard finds the preflight job by that word, so
    dropping it would hide the job from the guard.
    """
    keyed: set[str] = set()
    legacy: list[str] = []
    for path in sorted((ROOT / ".github/workflows").glob("*.y*ml")):
        text = path.read_text()
        jobs = (yaml.safe_load(text) or {}).get("jobs") or {}
        if JOB_KEY in jobs:
            keyed.add(path.name)
        if path.name in LEGACY_KEYED:
            continue
        for job_id, job in jobs.items():
            if job_id == "occ-preflight" or "occ-preflight" in _needs(job or {}):
                legacy.append(f"{path.name}:{job_id}")
        if "needs.occ-preflight." in text:
            legacy.append(f"{path.name}:needs.occ-preflight expression")
    assert "preflight" in JOB_KEY
    assert keyed == set(POLLERS) - set(LEGACY_KEYED)
    assert not legacy, legacy
