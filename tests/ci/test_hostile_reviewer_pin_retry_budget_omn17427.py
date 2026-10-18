# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: the Hostile Reviewer clones omniintelligence at the larger retry budget.

The review voter asks for 4096 tokens. When a reply does not parse as review
JSON it retries once, and before omniintelligence#1072 that retry got the same
4096-token cap, so a reply cut off at the cap failed the vote again and the
verdict degraded to ``degraded_quorum``. omniintelligence#1072 gives the retry
8192 tokens. The gate runs the omniintelligence checked out by the workflow, so
the fix reaches this repository's reviewers only when the clone pin moves to a
revision that carries it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "hostile-reviewer.yml"
)
# OMN-20910: the Claude API voter revision descends from the prior fix.
RETRY_BUDGET_PIN = "7f686d4bbca251dd3db49c1222ff8d6f738abd6c"
# The pin before this change, which retries at the same 4096-token cap.
PRE_RETRY_BUDGET_PIN = "64b950973a5686d33e0b439fb80749d66362d06d"
_PIN = re.compile(
    r"clone_with_retry omniintelligence \S+ OMNIINTELLIGENCE_SHA ([0-9a-f]{40})"
)


def omniintelligence_pins(workflow_text: str) -> list[str]:
    """Every full-sha pin the workflow clones omniintelligence at."""
    return _PIN.findall(workflow_text)


def test_omniintelligence_is_pinned_to_the_retry_budget_revision() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    pins = omniintelligence_pins(text)
    assert pins == [RETRY_BUDGET_PIN]


def test_the_pin_before_the_retry_budget_is_caught() -> None:
    """Positive control: the workflow as it was before this change."""
    text = WORKFLOW.read_text(encoding="utf-8")
    before = text.replace(omniintelligence_pins(text)[0], PRE_RETRY_BUDGET_PIN)
    assert omniintelligence_pins(before) == [PRE_RETRY_BUDGET_PIN]
    assert omniintelligence_pins(before)[0] != RETRY_BUDGET_PIN
