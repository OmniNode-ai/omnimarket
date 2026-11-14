# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pin the wiring of the effect-node bypass ratchet (OMN-24432): hook, CI job, strict summary row and the baseline.

Follows tests/ci/test_direct_model_call_ci_gate.py. The ratchet is only a ratchet while all four exist and name the same
module and the same baseline file. The last test reads the real tree: today's count may not exceed the baseline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_effect_bypass_ratchet_compute.models import (
    EnumBypassCategory,
    ModelBypassBaseline,
)
from omnimarket.nodes.node_effect_bypass_scan_effect.runtime_bypass_scan import main
from scripts.ci.ci_summary_gate import STRICT_GATE_JOBS

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[2]
JOB_ID = "effect-bypass-ratchet"
JOB_NAME = "Effect Bypass Ratchet (OMN-24432)"
HOOK_ID = "effect-bypass-ratchet"
MODULE = "omnimarket.nodes.node_effect_bypass_scan_effect"
BASELINE = ".onex_ratchets/effect_bypass_ceiling.yaml"


def _job() -> dict[str, Any]:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    )
    assert JOB_ID in workflow["jobs"], f"ci.yml has no job {JOB_ID!r}"
    job: dict[str, Any] = workflow["jobs"][JOB_ID]
    return job


def _job_text() -> str:
    return "\n".join(str(step.get("run", "")) for step in _job()["steps"])


def _hook() -> dict[str, Any]:
    config = yaml.safe_load(
        (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    )
    for repo in config["repos"]:
        for hook in repo["hooks"]:
            if hook["id"] == HOOK_ID:
                found: dict[str, Any] = hook
                return found
    raise AssertionError(f"no pre-commit hook {HOOK_ID!r}")


def test_job_is_named_and_unconditional() -> None:
    job = _job()
    assert job["name"] == JOB_NAME
    assert "needs" not in job
    assert "if" not in job


def test_job_is_a_strict_ci_summary_gate() -> None:
    assert JOB_NAME in STRICT_GATE_JOBS


def test_job_and_hook_run_the_same_module_against_the_same_baseline() -> None:
    hook = _hook()
    for text in (_job_text(), str(hook["entry"])):
        assert MODULE in text
        assert BASELINE in text
        assert "check" in text
        assert "--repository omnimarket" in text


def test_job_holds_the_merge_base_copy_of_the_baseline() -> None:
    text = _job_text()
    assert "--base-baseline" in text
    assert "git merge-base" in text


def test_job_runs_the_failing_control_tests() -> None:
    text = _job_text()
    assert "tests/unit/nodes/node_effect_bypass_scan_effect" in text
    assert "tests/unit/nodes/node_effect_bypass_ratchet_compute" in text


def test_job_has_no_swallowed_failure() -> None:
    job = _job()
    assert "continue-on-error" not in job
    assert all("continue-on-error" not in step for step in job["steps"])
    assert "|| true" not in _job_text()


def test_baseline_lists_every_category_for_every_repository() -> None:
    baseline = ModelBypassBaseline.model_validate(
        yaml.safe_load((REPO_ROOT / BASELINE).read_text(encoding="utf-8"))
    )
    assert baseline.ticket == "OMN-24432"
    assert "omnimarket" in baseline.repositories
    for repo, counts in baseline.repositories.items():
        assert set(counts.counts) == set(EnumBypassCategory), repo


def test_the_real_tree_is_at_or_under_its_baseline(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = main(
        [
            "check",
            "--repository",
            "omnimarket",
            "--root",
            str(REPO_ROOT),
            "--baseline",
            str(REPO_ROOT / BASELINE),
        ]
    )
    out = capsys.readouterr().out
    assert code == 0, out
