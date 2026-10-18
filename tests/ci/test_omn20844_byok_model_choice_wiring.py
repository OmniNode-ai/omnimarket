# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The customer-chooses-the-OpenRouter-model tests are enforced, not incidental.

Three surfaces carry them, and each is asserted here: a check-only pre-commit
hook, a named job in ``ci.yml`` (the workflow whose ``CI Summary`` job is the
required context), and that job's registration in ``STRICT_GATE_JOBS`` so a
skipped or absent conclusion fails closed.
"""

from __future__ import annotations

import re
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

JOB_KEY = "byok-customer-chooses-model"
JOB_NAME = "BYOK Customer Chooses Model (OMN-20844)"
GUARD_TESTS = (
    "tests/unit/cli/test_omn20844_openrouter_model_choice.py",
    "tests/test_omn20844_chosen_model_never_re_aimed.py",
    "tests/unit/nodes/node_delegate_skill_orchestrator/test_delegate_model_choice_omn20844.py",
    "tests/unit/cli/test_omn20844_onboarding_default_model_overlay.py",
)


def _hook() -> dict[str, object]:
    config = yaml.safe_load(PRECOMMIT.read_text(encoding="utf-8"))
    return next(
        hook
        for repo in config["repos"]
        for hook in repo["hooks"]
        if hook["id"] == JOB_KEY
    )


def _job() -> dict[str, Any]:
    job: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"][
        JOB_KEY
    ]
    return job


def _strict_jobs() -> tuple[str, ...]:
    source = CI_SUMMARY_GATE.read_text(encoding="utf-8")
    match = re.search(
        r"STRICT_GATE_JOBS:\s*tuple\[str, \.\.\.\]\s*=\s*\((?P<body>.*?)\n\)",
        source,
        re.S,
    )
    assert match is not None
    return tuple(re.findall(r'^\s*"([^"]+)",', match["body"], re.M))


def test_the_hook_runs_the_guard_tests_and_rewrites_nothing() -> None:
    hook = _hook()
    argv = shlex.split(str(hook["entry"]))
    assert argv[:3] == ["uv", "run", "pytest"]
    assert set(GUARD_TESTS) <= set(argv)
    assert "-x" not in argv
    assert "--lf" not in argv
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


def test_the_job_is_a_strict_gate_of_ci_summary() -> None:
    assert JOB_NAME in _strict_jobs()
