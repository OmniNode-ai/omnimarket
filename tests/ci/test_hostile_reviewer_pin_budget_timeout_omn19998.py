# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: an exhausted review budget fails the voter as a timeout.

The Hostile Reviewer now pins the chunk-scaled review deadline revision
(OMN-20910), which descends from the budget-timeout fix in omniintelligence#1095,
037af72c2a7f49fda953fed6a99edd5ec588dede. That fix descends from the
previous pin 3502f0de2f9be25cf68e3dc5877d9cb5580c90e0 (#1090). At the
old pin, exhausting a voter's shared review budget makes the adapter raise a
plain TimeoutError("review budget exhausted; review is incomplete"). The
substitutable_failure function in node_review_receipt_substitute_compute counts
a vote as timed out only when its error carries ONEX_CORE_092_TIMEOUT_ERROR,
so the lost required voter is ineligible for substitute receipts and cli_review
exits 2 with NO VERDICT. The new pin raises an InfraTimeoutError reading
"[ONEX_CORE_092_TIMEOUT_ERROR] review budget exhausted; review is incomplete
(model '<key>')", making the lost voter eligible for substitute receipts.
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
PRE_PIN = "3502f0de2f9be25cf68e3dc5877d9cb5580c90e0"


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
