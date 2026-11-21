# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The receipt-gate pin reads per-test duration records as test bookkeeping.

The omnibase_core receipt gate first counted config/test_file_durations.d records
as test-side at its dev-merge tag dev-20261120161913-d3ba14c48d02. Before that, a
pull request of tests, its contract and the duration record lint requires was read
as a product change. The caller must pin that tag or a later one.
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
FIRST_TAG_WITH_DURATION_RECORDS = "dev-20261120161913-d3ba14c48d02"
_DEV_MERGE_TAG = re.compile(r"^dev-(\d{14})-[0-9a-f]{12}$")


def _stamp(tag: str) -> int:
    match = _DEV_MERGE_TAG.match(tag)
    assert match, f"{tag!r} is not an omnibase_core dev-merge tag"
    return int(match.group(1))


def test_the_caller_pins_a_receipt_gate_that_reads_duration_records() -> None:
    pinned = re.findall(
        re.escape(RECEIPT_GATE_REUSABLE) + r"@(\S+)", CALLER.read_text()
    )
    assert pinned == [RECEIPT_GATE_PIN_TAG]
    assert _stamp(RECEIPT_GATE_PIN_TAG) >= _stamp(FIRST_TAG_WITH_DURATION_RECORDS)
