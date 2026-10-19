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
# OMN-17427: the overflow-deadline revision descends from the chunk-scaled
# review deadline revision.
OVERFLOW_DEADLINE_PIN = "921b322b8e43f695961a06a04759dc88bb3e88ff"
# The pin before this change, which cuts the overflow at its chunk share.
PRE_OVERFLOW_DEADLINE_PIN = "aeba95945f22b3027d4010b53cb01b3f1217923c"
_PIN = re.compile(
    r"clone_with_retry omniintelligence \S+ OMNIINTELLIGENCE_SHA ([0-9a-f]{40})"
)


def omniintelligence_pins(workflow_text: str) -> list[str]:
    """Every full-sha pin the workflow clones omniintelligence at."""
    return _PIN.findall(workflow_text)


def test_omniintelligence_is_pinned_to_the_overflow_deadline_revision() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    pins = omniintelligence_pins(text)
    assert pins == [OVERFLOW_DEADLINE_PIN]


def test_the_pin_before_the_overflow_deadline_is_caught() -> None:
    """Positive control: the workflow as it was before this change."""
    text = WORKFLOW.read_text(encoding="utf-8")
    before = text.replace(omniintelligence_pins(text)[0], PRE_OVERFLOW_DEADLINE_PIN)
    assert omniintelligence_pins(before) == [PRE_OVERFLOW_DEADLINE_PIN]
    assert omniintelligence_pins(before)[0] != OVERFLOW_DEADLINE_PIN
