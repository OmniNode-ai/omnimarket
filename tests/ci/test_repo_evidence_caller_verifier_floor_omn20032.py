# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20032: pin the receipt-gate reusable that refuses a verifier below the floor."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from tests.ci.receipt_gate_pin import RECEIPT_GATE_PIN_TAG

pytestmark = pytest.mark.unit

CALLER_PATH = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "call-repo-evidence-gate.yml"
)

# omnibase_core#1932 added VERIFIER_FLOOR to the reusable's "Install the pinned
# verifier" step; omnibase_core#2203 (OMN-21676) turned it into an ancestry floor
# for verifier-tag. The pin moved to a descendant that keeps this.
_FLOOR_REUSABLE_TAG = RECEIPT_GATE_PIN_TAG

# OMN-21676: the reusable decides the floor by ancestry, not by version number.
# VERIFIER_FLOOR_SHA is omnimarket#3511's squash commit, the first to refuse a
# contract that leaves an acceptance criterion unbound; the reusable refuses a
# verifier-tag that does not descend from it. The caller therefore passes a
# dev-merge tag as verifier-tag and no verifier-version.
_DEV_MERGE_TAG = r"dev-[0-9]{14}-[0-9a-f]{12}"


def _job() -> dict[str, object]:
    caller = yaml.safe_load(CALLER_PATH.read_text(encoding="utf-8"))
    job: dict[str, object] = caller["jobs"]["repo-evidence"]
    return job


def test_caller_pins_the_reusable_that_carries_the_verifier_floor() -> None:
    uses = _job()["uses"]
    assert isinstance(uses, str)
    assert uses.endswith(f"@{_FLOOR_REUSABLE_TAG}"), (
        "pin the omnibase_core receipt-gate reusable at or past omnibase_core#2203"
    )


def test_caller_verifier_tag_is_passed_to_the_floor_enforcing_reusable() -> None:
    job = _job()
    assert job["uses"] == (
        "OmniNode-ai/omnibase_core/.github/workflows/receipt-gate.yml"
        f"@{_FLOOR_REUSABLE_TAG}"
    ), "the verifier tag must be passed to the floor-enforcing reusable"
    with_inputs = job["with"]
    assert isinstance(with_inputs, dict)
    assert re.fullmatch(_DEV_MERGE_TAG, with_inputs["verifier-tag"]), (
        "verifier-tag must be an omnimarket dev-merge tag"
    )
    assert "verifier-version" not in with_inputs, (
        "verifier-version is a warned migration fallback, not a caller input"
    )


@pytest.mark.parametrize(
    ("verifier", "expected"),
    [
        ("0.4.305", False),
        ("dev-20261101000200-949993edf3b", False),
        ("dev-20261101000200-949993edf3b7", True),
    ],
)
def test_verifier_tag_shape_has_an_admitting_and_refusing_cases(
    verifier: str, expected: bool
) -> None:
    assert (re.fullmatch(_DEV_MERGE_TAG, verifier) is not None) is expected
