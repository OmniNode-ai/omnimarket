# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The merge queue gets the repo-evidence context dev requires (OMN-20497).

dev's repo-evidence ruleset requires `repo-evidence / dod-verify`. Its caller
runs on pull_request_target and its reusable refuses any other event, so a queue
commit carries the context only through the merge-group shim workflow. These
tests read this repository's own workflow files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github" / "workflows"
CONTEXT = "repo-evidence / dod-verify"
pytestmark = pytest.mark.unit


def _load(name: str) -> dict[Any, Any]:
    loaded = yaml.safe_load((WORKFLOWS / name).read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_caller_names_the_context_the_ruleset_requires() -> None:
    caller = _load("call-repo-evidence-gate.yml")
    assert "repo-evidence" in caller["jobs"]
    assert "merge_group" not in caller[True]


def test_queue_commit_reports_the_repo_evidence_context() -> None:
    shim = _load("merge-group-pr-scoped-contexts.yml")
    names = [job.get("name") for job in shim["jobs"].values()]
    assert names.count(CONTEXT) == 1


def test_repo_evidence_shim_runs_on_merge_group_only() -> None:
    shim = _load("merge-group-pr-scoped-contexts.yml")
    assert set(shim[True]) == {"merge_group"}
