# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Hostile Reviewer's second voter URL is a repository variable (OMN-20422).

RULING 2026-10-10T16:28:58Z: one model on the Mac Studio; the second Studio reviewer seat
is retired. The variable names the Studio's one served model, so a model swap there is a
variable change, not a pull request, and no lab address is written into this public file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit]

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "hostile-reviewer.yml"
)


def _job_env() -> dict[str, str]:
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    env = workflow["jobs"]["hostile-review"].get("env")
    assert isinstance(env, dict), "hostile-review job must carry a job-level env"
    return env


def test_second_voter_url_is_the_repository_variable() -> None:
    assert (
        _job_env()["LLM_LOCAL_STUDIO_PLANNER_URL"]
        == "${{ vars.LLM_LOCAL_STUDIO_PLANNER_URL }}"
    )


def test_cidr_allowlist_is_job_level() -> None:
    assert _job_env()["LLM_ENDPOINT_CIDR_ALLOWLIST"]


def test_both_voter_models_are_still_requested() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "--model qwen3-review" in text
    assert "--model local-studio-planner" in text
