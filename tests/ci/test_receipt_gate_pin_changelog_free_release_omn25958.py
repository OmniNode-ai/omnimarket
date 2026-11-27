# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The receipt-gate pin accepts a changelog-free release cut.

The omnibase_core receipt gate first exempted a writer-app release pull request that
changes no file, for a repository with no root CHANGELOG.md, at its dev-merge tag
dev-20261122081426-c5dc44467b01. Before that, this repository's release pull requests (the
train writes no CHANGELOG.md here since OMN-25958) were refused as ticket work.
The caller must pin that tag or a later one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.ci.receipt_gate_pin import RECEIPT_GATE_PIN_TAG, RECEIPT_GATE_REUSABLE

pytestmark = pytest.mark.unit

CALLER = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "call-repo-evidence-gate.yml"
)
FIRST_TAG_WITH_CHANGELOG_FREE_RELEASE = "dev-20261122081426-c5dc44467b01"
_DEV_MERGE_TAG = re.compile(r"^dev-(\d{14})-[0-9a-f]{12}$")


def _stamp(tag: str) -> int:
    match = _DEV_MERGE_TAG.match(tag)
    assert match, f"{tag!r} is not an omnibase_core dev-merge tag"
    return int(match.group(1))


def test_the_caller_pins_a_receipt_gate_that_accepts_a_changelog_free_release() -> None:
    pinned = re.findall(
        re.escape(RECEIPT_GATE_REUSABLE) + r"@(\S+)", CALLER.read_text()
    )
    assert pinned == [RECEIPT_GATE_PIN_TAG]
    assert _stamp(RECEIPT_GATE_PIN_TAG) >= _stamp(FIRST_TAG_WITH_CHANGELOG_FREE_RELEASE)
