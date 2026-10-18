# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Public-repository runner placement in omnimarket (OMN-19998).

RULING 2026-10-16T07:07:09Z: "Ci runners should be for private repos only."
omnimarket is public, so:

* no routing-seam fallback in a workflow names the self-hosted fleet, except
  the two lab carve-outs (the hostile reviewer calls lab models; the PR merged
  publisher writes to the tailnet-only dev-lane broker), which stay until they
  move off GitHub Actions;
* omnibase_core's check-public-runner-placement runs as a CI job that CI
  Summary registers and as a pre-commit hook, at the same core pin.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CI_WORKFLOW = WORKFLOWS / "ci.yml"
PRECOMMIT = REPO_ROOT / ".pre-commit-config.yaml"
CI_SUMMARY_GATE = REPO_ROOT / "scripts" / "ci" / "ci_summary_gate.py"

FLEET_FALLBACK = """'["self-hosted","omnibase-ci"]'"""
LAB_BROKER_CARVE_OUTS = (
    "OMNI_REVIEW_RUNS_ON_JSON",
    "OMNI_PR_MERGED_PUBLISHER_RUNS_ON_JSON",
)
JOB_NAME = "Public Runner Placement (OMN-19998)"
HOOK_ID = "check-public-runner-placement"
CORE_RELEASE = "0.47.46"
CORE_RELEASE_SHA = "c880801d4deeaa1627535c8622ab6c4ab8cfbecd"


def _load(path: Path) -> dict[object, object]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def fleet_fallbacks(text: str) -> list[str]:
    """Code lines whose routing fallback is the self-hosted fleet, carve-outs excluded."""
    return [
        line.strip()
        for line in text.splitlines()
        if FLEET_FALLBACK in line
        and not line.lstrip().startswith("#")
        and not any(knob in line for knob in LAB_BROKER_CARVE_OUTS)
    ]


def test_fleet_fallback_detector_positive_control() -> None:
    planted = (
        "    runs-on: ${{ fromJSON(vars.OMNI_TRUSTED_CI_RUNS_ON_JSON || "
        """'["self-hosted","omnibase-ci"]') }}\n"""
    )
    carve_out = (
        "        || fromJSON(vars.OMNI_REVIEW_RUNS_ON_JSON || "
        """'["self-hosted","omnibase-ci"]')\n"""
    )
    hosted = (
        "    runs-on: ${{ fromJSON(vars.OMNI_TRUSTED_CI_RUNS_ON_JSON || "
        """'["ubuntu-latest"]') }}\n"""
    )
    assert fleet_fallbacks(planted) == [planted.strip()]
    assert fleet_fallbacks(carve_out) == []
    assert fleet_fallbacks(hosted) == []


def test_no_workflow_falls_back_to_the_self_hosted_fleet() -> None:
    offenders = {
        path.name: found
        for path in sorted(WORKFLOWS.glob("*.y*ml"))
        if (found := fleet_fallbacks(path.read_text(encoding="utf-8")))
    }
    assert offenders == {}, (
        "A public repository's routing fallback must be GitHub-hosted "
        "(RULING 2026-10-16T07:07:09Z): " + repr(offenders)
    )


def _ci_job() -> dict[str, object]:
    jobs = _load(CI_WORKFLOW)["jobs"]
    assert isinstance(jobs, dict)
    job = jobs["public-runner-placement"]
    assert isinstance(job, dict)
    return job


def _hook_rev() -> str:
    repos = _load(PRECOMMIT)["repos"]
    assert isinstance(repos, list)
    revs = [
        repo["rev"]
        for repo in repos
        if isinstance(repo, dict)
        and any(
            isinstance(hook, dict) and hook.get("id") == HOOK_ID
            for hook in repo.get("hooks", [])
        )
    ]
    assert len(revs) == 1, f"expected one {HOOK_ID} hook, found {len(revs)}"
    return str(revs[0])


def test_ci_job_runs_the_core_check_unconditionally_on_a_hosted_runner() -> None:
    job = _ci_job()
    assert job["name"] == JOB_NAME
    assert job["runs-on"] == "ubuntu-latest"
    assert "needs" not in job
    assert "if" not in job
    steps = job["steps"]
    assert isinstance(steps, list)
    runs = "\n".join(str(step.get("run", "")) for step in steps)
    assert "omnibase_core.handlers.handler_public_runner_placement_cli" in runs
    assert "POSITIVE CONTROL FAILED" in runs
    assert "--base-ref" in runs


def test_ci_job_and_hook_run_the_same_core_release() -> None:
    """The CI job installs 0.47.46; the hook's rev is that release's tagged commit."""
    env = _ci_job()["env"]
    assert isinstance(env, dict)
    assert env["RUNNER_PLACEMENT_CORE"] == f"omnibase-core=={CORE_RELEASE}"
    assert _hook_rev() == CORE_RELEASE_SHA


def test_ci_summary_registers_the_job() -> None:
    assert f'"{JOB_NAME}",' in CI_SUMMARY_GATE.read_text(encoding="utf-8")


def test_precommit_hook_names_this_public_repository() -> None:
    repos = _load(PRECOMMIT)["repos"]
    assert isinstance(repos, list)
    hooks = [
        hook
        for repo in repos
        if isinstance(repo, dict)
        for hook in repo.get("hooks", [])
        if isinstance(hook, dict) and hook.get("id") == HOOK_ID
    ]
    assert hooks
    assert hooks[0]["args"] == ["--repo", "omnimarket", "--visibility", "public"]
