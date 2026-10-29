# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21500 T2.1: omnimarket consumes omnibase_core's validators by hook id.

The rules live in omnibase_core. This repository owns only the wiring: each
validator is a hook of the shared core hook repository at a dev-merge tag, and the
same hooks run in CI as a job that CI Summary registers.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PRECOMMIT = REPO_ROOT / ".pre-commit-config.yaml"
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
CI_SUMMARY_GATE = REPO_ROOT / "scripts" / "ci" / "ci_summary_gate.py"

CORE_URL = "https://github.com/OmniNode-ai/omnibase_core"
DEV_MERGE_TAG = re.compile(r"dev-\d{14}-[0-9a-f]{12}")
CI_JOB_ID = "core-validators"
CI_JOB_NAME = "Core Validators (OMN-21500)"

CORE_HOOKS = (
    "check-no-utcnow",
    "check-no-raw-sqlite3",
    "check-no-direct-kafka-producer",
    "check-local-paths-compute",
    "check-hardcoded-model-config-compute",
    "check-ai-slop",
)


def _repos() -> list[dict[str, Any]]:
    loaded = yaml.safe_load(PRECOMMIT.read_text(encoding="utf-8"))
    repos = loaded["repos"]
    assert isinstance(repos, list)
    return repos


def _hook(url: str, hook_id: str) -> tuple[str, dict[str, Any]]:
    found = [
        (str(repo["rev"]), hook)
        for repo in _repos()
        if repo.get("repo") == url
        for hook in repo["hooks"]
        if hook.get("id") == hook_id
    ]
    assert len(found) == 1, f"{hook_id}: expected one hook of {url}, found {len(found)}"
    return found[0]


@pytest.mark.parametrize("hook_id", CORE_HOOKS)
def test_core_hook_is_consumed_at_a_dev_merge_tag(hook_id: str) -> None:
    rev, _ = _hook(CORE_URL, hook_id)
    assert DEV_MERGE_TAG.fullmatch(rev), (
        f"{hook_id}: rev {rev!r} is not a dev-merge tag"
    )


def test_core_hooks_share_one_tag() -> None:
    revs = {_hook(CORE_URL, hook_id)[0] for hook_id in CORE_HOOKS}
    assert len(revs) == 1, f"core validators pinned at several tags: {sorted(revs)}"


def test_model_config_hook_declares_no_args_so_the_manifest_baseline_applies() -> None:
    _, hook = _hook(CORE_URL, "check-hardcoded-model-config-compute")
    assert "args" not in hook


def test_model_config_baseline_is_committed() -> None:
    baseline = REPO_ROOT / "config" / "hardcoded_model_config_baseline.yaml"
    assert baseline.is_file()
    with baseline.open(encoding="utf-8") as stream:
        loaded = yaml.safe_load(stream)
    assert isinstance(loaded, dict), "expected a YAML mapping"
    entries = loaded["entries"]
    assert isinstance(entries, list)
    assert entries, "expected a non-empty baseline entries list"


def test_canonical_file_shape_hook_names_the_tag_of_its_ci_job() -> None:
    rev, _ = _hook(CORE_URL, "canonical-file-shape")
    jobs = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    core = jobs["canonical-file-shape"]["env"]["CANONICAL_FILE_SHAPE_CORE"]
    assert core.endswith(f"@{rev}"), f"hook rev {rev!r} differs from CI pin {core!r}"


def _ci_job() -> dict[str, Any]:
    jobs = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    job = jobs[CI_JOB_ID]
    assert isinstance(job, dict)
    return job


def test_ci_job_runs_every_hook_unconditionally_on_a_hosted_runner() -> None:
    job = _ci_job()
    assert job["name"] == CI_JOB_NAME
    assert job["runs-on"] == "ubuntu-latest"
    assert "needs" not in job
    assert "if" not in job
    runs = "\n".join(str(step.get("run", "")) for step in job["steps"])
    for hook_id in CORE_HOOKS:
        assert hook_id in runs, f"ci.yml {CI_JOB_ID} does not run {hook_id}"
    assert "--from-ref" in runs
    assert "POSITIVE CONTROL FAILED" in runs


def test_ci_summary_registers_the_job() -> None:
    assert f'"{CI_JOB_NAME}",' in CI_SUMMARY_GATE.read_text(encoding="utf-8")
