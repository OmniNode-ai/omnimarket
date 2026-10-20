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
# OMN-20910: the revision is no longer a literal in the workflow. It is the newest
# dev-* tag of omniintelligence, resolved at run time, and every revision tagged
# since the fix carries it. A sha literal or a branch tip is refused.
_REF = re.compile(r"clone_with_retry omniintelligence \S+ OMNIINTELLIGENCE_SHA (\S+)")
NEWEST_DEV_TAG_REF = '"refs/tags/${OMNIINTELLIGENCE_TAG}"'
PRE_PIN = "64b950973a5686d33e0b439fb80749d66362d06d"


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
