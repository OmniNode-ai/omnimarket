# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19998: an exhausted review budget fails the voter as a timeout.

The Hostile Reviewer now pins the Claude API voter revision (OMN-20910),
which descends from the budget-timeout fix in omniintelligence#1095,
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
# OMN-20910: the Claude API voter revision retains the budget-timeout fix.
BUDGET_TIMEOUT_PIN = "7f686d4bbca251dd3db49c1222ff8d6f738abd6c"
PRE_BUDGET_TIMEOUT_PIN = "3502f0de2f9be25cf68e3dc5877d9cb5580c90e0"
_PIN = re.compile(
    r"clone_with_retry omniintelligence \S+ OMNIINTELLIGENCE_SHA ([0-9a-f]{40})"
)


def omniintelligence_pins(workflow_text: str) -> list[str]:
    """Every full-sha pin the workflow clones omniintelligence at."""
    return _PIN.findall(workflow_text)


def test_omniintelligence_is_pinned_to_the_budget_timeout_revision() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    pins = omniintelligence_pins(text)
    assert pins == [BUDGET_TIMEOUT_PIN]


def test_the_pin_before_budget_timeout_is_caught() -> None:
    """Positive control: the workflow as it was before this change."""
    text = WORKFLOW.read_text(encoding="utf-8")
    before = text.replace(omniintelligence_pins(text)[0], PRE_BUDGET_TIMEOUT_PIN)
    pins = omniintelligence_pins(before)
    assert pins == [PRE_BUDGET_TIMEOUT_PIN]
    assert pins != [BUDGET_TIMEOUT_PIN]
