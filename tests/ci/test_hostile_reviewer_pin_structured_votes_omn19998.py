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
# OMN-20910: the revision is no longer a literal in the workflow. It is the newest
# dev-* tag of omniintelligence, resolved at run time, and every revision tagged
# since the fix carries it. A sha literal or a branch tip is refused.
_REF = re.compile(r"clone_with_retry omniintelligence \S+ OMNIINTELLIGENCE_SHA (\S+)")
NEWEST_DEV_TAG_REF = '"refs/tags/${OMNIINTELLIGENCE_TAG}"'
PRE_PIN = "3fcbf795087a79081c0ceeb676216bf64c5d49ae"


def omniintelligence_refs(workflow_text: str) -> list[str]:
    """Every ref the workflow clones omniintelligence at."""
    return _REF.findall(workflow_text)


def test_omniintelligence_is_read_at_its_newest_dev_tag() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert omniintelligence_refs(text) == [NEWEST_DEV_TAG_REF]


def test_the_pin_before_this_change_is_caught() -> None:
    """Positive control: the workflow as it was, pinned to a sha."""
    text = WORKFLOW.read_text(encoding="utf-8")
    before = text.replace(NEWEST_DEV_TAG_REF, PRE_PIN)
    assert omniintelligence_refs(before) == [PRE_PIN]
    assert omniintelligence_refs(before) != [NEWEST_DEV_TAG_REF]
