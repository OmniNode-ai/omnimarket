# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Hostile Reviewer runs the omniintelligence build that fails an unparseable vote (OMN-20422).

omniintelligence#1016 made an unparseable reviewer reply a failed vote instead
of a clean review, and constrained the second voter's decoding to a findings
schema. Nothing consumes that until this workflow's pin moves. The same bump
carries the registry key rename of omniintelligence#1014: the second voter is
``local-studio-planner`` and its URL comes from ``LLM_LOCAL_STUDIO_PLANNER_URL``.
"""

from __future__ import annotations

import re
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
# The dev tip of omniintelligence that carries #1014 (key rename) and #1016
# (unparseable reply = failed vote; json_schema constraint on the second voter).
PARSE_FIX_SHA = "33a29cdfba6f14be6f94c3c77a05e29f345c692b"
RETIRED_KEY = "gpt-oss-review"
RETIRED_ENV = "LLM_GPT_OSS_REVIEW_URL"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_omniintelligence_is_pinned_to_the_parse_fix() -> None:
    pins = re.findall(
        r"clone_with_retry\s+omniintelligence\s+\S+\s+\S+\s+([0-9a-f]{40})\b", _text()
    )
    assert pins == [PARSE_FIX_SHA]


def test_the_retired_key_and_env_var_are_gone() -> None:
    text = _text()
    assert RETIRED_KEY not in text
    assert RETIRED_ENV not in text


def test_second_voter_url_is_the_registry_env_var_on_8131() -> None:
    workflow = yaml.safe_load(_text())
    env = workflow["jobs"]["hostile-review"]["env"]
    assert env["LLM_LOCAL_STUDIO_PLANNER_URL"].endswith(":8131")


def test_every_roster_site_names_the_renamed_key() -> None:
    text = _text()
    # The retry-budget invariant and the review itself pass it as a flag line.
    flags = re.findall(r"^\s+--model local-studio-planner \\$", text, re.MULTILINE)
    assert len(flags) == 2
    assert 'REVIEW_MODEL_KEYS: "qwen3-review local-studio-planner"' in text
