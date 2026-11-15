"""The direct-model-call gate keeps timeout room over its seven repository scans (OMN-24409).

The job runs the whole-repository scan seven times (four planted refusals, two planted
accepts, the real check), about two minutes each on a hosted runner. A 15 minute limit
left under one minute of slack; the dev push run for the OMN-24409 merge was cancelled
at the limit and the CI Summary job read red on the base branch.
"""

from __future__ import annotations

from pathlib import Path

import yaml

CI_YML = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
JOB_ID = "direct-model-call-gate"
# Seven scans at about two minutes each, plus checkout and tool install, doubled for runner variance.
MIN_TIMEOUT_MINUTES = 30


def _job() -> dict[str, object]:
    jobs = yaml.safe_load(CI_YML.read_text(encoding="utf-8"))["jobs"]
    assert JOB_ID in jobs, f"{JOB_ID} is missing from ci.yml"
    job = jobs[JOB_ID]
    assert isinstance(job, dict)
    return job


def test_direct_model_call_gate_timeout_has_room() -> None:
    timeout = _job().get("timeout-minutes")
    assert isinstance(timeout, int), "the job must declare timeout-minutes"
    assert timeout >= MIN_TIMEOUT_MINUTES, (
        f"timeout-minutes is {timeout}; seven repository scans need at least "
        f"{MIN_TIMEOUT_MINUTES} so a loaded push run is not cancelled"
    )
