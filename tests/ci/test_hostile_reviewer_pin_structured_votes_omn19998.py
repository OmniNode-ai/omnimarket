# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: the Hostile Reviewer keeps large reviewer votes structured.

The Hostile Reviewer clones omniintelligence at the revision that keeps large
reviewer votes structured. At 3fcbf795 a voter whose reply is free text instead
of review JSON (replies of 19245 and 34196 chars, or 172 and 190 chars) fails
its vote twice and a required voter's lost vote leaves the gate degraded.
3502f0de2f9be25cf68e3dc5877d9cb5580c90e0 (omniintelligence dev) sizes the output
budget to the evidence, splits a diff larger than the voter's context window
into chunks, retries an unparseable reply as JSON only with thinking off, and
counts only a complete review as a vote. The gate runs the omniintelligence
the workflow clones, so the fix reaches this repository only through the pin.
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
# OMN-20910: the chunk-scaled review deadline revision descends from the prior fix.
STRUCTURED_VOTES_PIN = "aeba95945f22b3027d4010b53cb01b3f1217923c"
PRE_STRUCTURED_VOTES_PIN = "3fcbf795087a79081c0ceeb676216bf64c5d49ae"
_PIN = re.compile(
    r"clone_with_retry omniintelligence \S+ OMNIINTELLIGENCE_SHA ([0-9a-f]{40})"
)


def omniintelligence_pins(workflow_text: str) -> list[str]:
    """Every full-sha pin the workflow clones omniintelligence at."""
    return _PIN.findall(workflow_text)


def test_omniintelligence_is_pinned_to_the_structured_votes_revision() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    pins = omniintelligence_pins(text)
    assert pins == [STRUCTURED_VOTES_PIN]


def test_the_pin_before_structured_votes_is_caught() -> None:
    """Positive control: the workflow as it was before this change."""
    text = WORKFLOW.read_text(encoding="utf-8")
    before = text.replace(omniintelligence_pins(text)[0], PRE_STRUCTURED_VOTES_PIN)
    pins = omniintelligence_pins(before)
    assert pins == [PRE_STRUCTURED_VOTES_PIN]
    assert pins[0] != STRUCTURED_VOTES_PIN
