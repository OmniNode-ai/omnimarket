# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: the Hostile Reviewer clones omniintelligence at the overflow-deadline fix.

The security-contracts voter calls a primary endpoint and, when that endpoint
is full, an overflow endpoint. Before omniintelligence#1108 the overflow, the
last endpoint of a chunk, was cut at half of that chunk's even share of the
vote budget, and every later chunk retried the full primary first. A busy
primary therefore exhausted the vote budget in tens of seconds and the voter
failed with "review budget exhausted", so the gate degraded on every PR.
omniintelligence#1108 (921b322b8e43, dev tag dev-20261019012929-921b322b8e43)
bounds the last endpoint by the vote's deadline and skips an endpoint that
already failed over in the same vote. The gate runs the omniintelligence
checked out by the workflow, so the fix reaches this repository's reviewers
only when the clone pin moves to a revision that carries it.
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
# since the overflow-deadline fix carries it. A sha literal or a branch tip is refused.
_REF = re.compile(r"clone_with_retry omniintelligence \S+ OMNIINTELLIGENCE_SHA (\S+)")
NEWEST_DEV_TAG_REF = '"refs/tags/${OMNIINTELLIGENCE_TAG}"'
# The pin before this change, which cuts the overflow at its chunk share.
PRE_OVERFLOW_DEADLINE_PIN = "aeba95945f22b3027d4010b53cb01b3f1217923c"


def omniintelligence_refs(workflow_text: str) -> list[str]:
    """Every ref the workflow clones omniintelligence at."""
    return _REF.findall(workflow_text)


def test_omniintelligence_is_read_at_its_newest_dev_tag() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert omniintelligence_refs(text) == [NEWEST_DEV_TAG_REF]


def test_the_pin_before_the_overflow_deadline_is_caught() -> None:
    """Positive control: the workflow as it was, pinned to the pre-fix sha."""
    text = WORKFLOW.read_text(encoding="utf-8")
    before = text.replace(NEWEST_DEV_TAG_REF, PRE_OVERFLOW_DEADLINE_PIN)
    assert omniintelligence_refs(before) == [PRE_OVERFLOW_DEADLINE_PIN]
    assert omniintelligence_refs(before) != [NEWEST_DEV_TAG_REF]
