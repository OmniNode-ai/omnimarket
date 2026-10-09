# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Hostile Reviewer's second voter is the Studio's :8131 server (OMN-20422)."""

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


def test_second_voter_url_points_at_studio_8131() -> None:
    assert _job_env()["LLM_GPT_OSS_REVIEW_URL"].endswith(":8131")


def test_cidr_allowlist_is_job_level() -> None:
    assert _job_env()["LLM_ENDPOINT_CIDR_ALLOWLIST"]


def test_both_voter_models_are_still_requested() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "--model qwen3-review" in text
    assert "--model gpt-oss-review" in text
