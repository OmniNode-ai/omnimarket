# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pin the CI half of the direct-model-call gate (OMN-20287).

The `check-direct-model-call` pre-commit hook refuses a model call outside the
delegation nodes at commit time. A hook can be skipped on a machine, so the
same module has to run in CI too, as a job that folds into the required
`CI Summary` context (STRICT_GATE_JOBS in scripts/ci/ci_summary_gate.py)
rather than as a new required context of its own. This module pins that the
job exists, that it runs the hook's own module at the hook's own omnibase_core
pin, that it plants every audit fixture and requires the refusal, and that
`CI Summary` fails closed if the job is skipped or absent.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.ci.ci_summary_gate import STRICT_GATE_JOBS

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[2]
PRECOMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "direct_model_call"

JOB_ID = "direct-model-call-gate"
JOB_NAME = "Direct Model Call Gate (OMN-20287)"
HOOK_ID = "check-direct-model-call"
MODULE = (
    "omnibase_core.nodes.node_direct_model_call_check_effect.runtime_direct_model_call"
)
BASELINE = ".onex_ratchets/direct_model_call_baseline.yaml"
REFUSED_FIXTURES = ("s5a", "s5c", "s5g", "s6")
CLEAN_FIXTURE = "clean"
SANCTIONED_NODE = "src/omnimarket/nodes/node_llm_delegation_call_effect"


def _job() -> dict[str, Any]:
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    jobs = workflow["jobs"]
    assert JOB_ID in jobs, f"ci.yml has no job {JOB_ID!r}"
    job: dict[str, Any] = jobs[JOB_ID]
    return job


def _job_text() -> str:
    job = _job()
    return (
        "\n".join(str(step.get("run", "")) for step in job["steps"])
        + "\n"
        + str(job.get("env", ""))
    )


def _hook_rev() -> str:
    config = yaml.safe_load(PRECOMMIT_CONFIG.read_text(encoding="utf-8"))
    for repo in config["repos"]:
        if any(hook["id"] == HOOK_ID for hook in repo["hooks"]):
            rev: str = repo["rev"]
            return rev
    raise AssertionError(f"no pre-commit hook {HOOK_ID!r}")


def test_job_is_named_and_unconditional() -> None:
    job = _job()
    assert job["name"] == JOB_NAME
    # An unconditional job with no `needs` always instantiates, so a skipped or
    # cancelled conclusion fails closed in CI Summary instead of going absent.
    assert "needs" not in job
    assert "if" not in job


def test_job_is_a_strict_ci_summary_gate() -> None:
    assert JOB_NAME in STRICT_GATE_JOBS


def test_job_runs_the_hooks_module_at_the_hooks_pin() -> None:
    text = _job_text()
    assert MODULE in text
    assert _hook_rev() in text, (
        "CI and the pre-commit hook must pin the same core commit"
    )


def test_job_scans_against_the_shrink_only_baseline_and_the_base_ref() -> None:
    text = _job_text()
    assert "--repo omnimarket" in text
    assert f"--baseline {BASELINE}" in text
    assert re.search(r'--base\s+"?\$', text), (
        "baseline growth must be refused against the base"
    )


def test_job_plants_every_refused_fixture_by_name() -> None:
    text = _job_text()
    for fixture in REFUSED_FIXTURES:
        assert (FIXTURE_DIR / f"{fixture}.py.txt").is_file()
        assert f"{fixture}.py.txt" in text, f"fixture {fixture} is never planted"


def test_job_requires_the_clean_fixture_and_the_delegation_node_to_pass() -> None:
    text = _job_text()
    assert CLEAN_FIXTURE in text
    assert SANCTIONED_NODE in text
    assert (REPO_ROOT / SANCTIONED_NODE).is_dir()


def test_job_has_no_swallowed_failure() -> None:
    job = _job()
    assert "continue-on-error" not in job
    assert all("continue-on-error" not in step for step in job["steps"])
