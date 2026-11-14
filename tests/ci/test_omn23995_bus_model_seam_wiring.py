# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus-model seam check is enforced on three surfaces, and each is asserted here.

A check-only pre-commit hook, a named job in ``ci.yml`` (the workflow whose
``CI Summary`` job is the required context), and that job's registration in
``STRICT_GATE_JOBS`` so a skipped or absent conclusion fails closed. The
policy file names the two incident models and holds no pin for either.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PRECOMMIT = REPO_ROOT / ".pre-commit-config.yaml"
CI_SUMMARY_GATE = REPO_ROOT / "scripts" / "ci" / "ci_summary_gate.py"
SEAM_POLICY = (
    REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_bus_model_seam_check_effect"
    / "seam_policy.yaml"
)

JOB_KEY = "bus-model-seam-gate"
JOB_NAME = "Bus Model Seam Gate (OMN-23995)"
GUARD_TESTS = (
    "tests/unit/nodes/node_bus_model_seam_check_effect",
    "tests/test_golden_chain_bus_model_seam_check_effect.py",
)
MODULE = "omnimarket.nodes.node_bus_model_seam_check_effect"
STRICT_MODELS = (
    "omnimarket.models.model_host_capacity_advertisement.ModelHostCapacityAdvertisement",
    "omnimarket.nodes.node_silent_failure_alarm_effect.models.ModelSilentFailureAlarmRequest",
)


HOOK_ID = "bus-model-seam-check"


def _hook() -> dict[str, object]:
    config = yaml.safe_load(PRECOMMIT.read_text(encoding="utf-8"))
    return next(
        hook
        for repo in config["repos"]
        for hook in repo["hooks"]
        if hook["id"] == HOOK_ID
    )


def _job() -> dict[str, Any]:
    job: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"][
        JOB_KEY
    ]
    return job


def _strict_jobs() -> tuple[str, ...]:
    doc = json.loads(
        CI_SUMMARY_GATE.with_name("ci_summary_rules.json").read_text(encoding="utf-8")
    )
    return tuple(e["name"] for e in doc["rules"]["STRICT_GATE_JOBS"]["entries"])


def _module_after_dash_m(argv: list[str]) -> str:
    return argv[argv.index("-m") + 1]


def test_the_hook_runs_the_guard_tests_and_rewrites_nothing() -> None:
    hook = _hook()
    argv = shlex.split(str(hook["entry"]))
    assert "python" in argv
    assert "-m" in argv
    assert MODULE in argv
    assert hook["pass_filenames"] is False
    assert "--fix" not in str(hook["entry"])


def test_the_ci_job_runs_the_same_tests_unconditionally() -> None:
    job = _job()
    assert job["name"] == JOB_NAME
    assert "if" not in job
    assert "needs" not in job
    run = " ".join(str(step.get("run", "")) for step in job["steps"])
    for test in GUARD_TESTS:
        assert test in run
    assert f"python -m {MODULE}" in run


def test_the_job_is_a_strict_gate_of_ci_summary() -> None:
    assert JOB_NAME in _strict_jobs()


def test_the_hook_and_the_ci_job_run_the_same_module() -> None:
    hook_argv = shlex.split(str(_hook()["entry"]))
    hook_module = _module_after_dash_m(hook_argv)
    job = _job()
    run = " ".join(str(step.get("run", "")) for step in job["steps"])
    job_module = _module_after_dash_m(shlex.split(run))
    assert hook_module == job_module == MODULE


def test_the_policy_names_the_two_incident_models() -> None:
    policy = yaml.safe_load(SEAM_POLICY.read_text(encoding="utf-8"))
    strict = policy["strict_models"]
    for model in STRICT_MODELS:
        assert model in strict
    pins = policy.get("pins") or []
    for pin in pins:
        assert pin.get("producer_model") not in STRICT_MODELS
        assert pin.get("consumer_model") not in STRICT_MODELS
