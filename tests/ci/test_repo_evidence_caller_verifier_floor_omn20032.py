# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20032: pin the receipt-gate reusable that refuses a verifier below the floor."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

CALLER_PATH = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "call-repo-evidence-gate.yml"
)

# omnibase_core#1932: the squash commit that adds VERIFIER_FLOOR to the reusable's
# "Install the pinned verifier" step.
_FLOOR_REUSABLE_SHA = "4e4f5e0404d364e296c5d37db6aa3ab9e07f8ffa"

# 0.4.303 is the first omnimarket release carrying omnimarket#3511, the refusal of
# a contract that leaves an acceptance criterion unbound.
_VERIFIER_FLOOR = (0, 4, 303)


def _version(text: str) -> tuple[int, ...]:
    return tuple(int(part) for part in text.split("."))


def _job() -> dict[str, object]:
    caller = yaml.safe_load(CALLER_PATH.read_text(encoding="utf-8"))
    job: dict[str, object] = caller["jobs"]["repo-evidence"]
    return job


def test_caller_pins_the_reusable_that_carries_the_verifier_floor() -> None:
    uses = _job()["uses"]
    assert isinstance(uses, str)
    assert uses.endswith(f"@{_FLOOR_REUSABLE_SHA}"), (
        "pin the omnibase_core receipt-gate reusable at or past omnibase_core#1932"
    )


def test_caller_verifier_version_is_at_or_above_the_floor() -> None:
    with_inputs = _job()["with"]
    assert isinstance(with_inputs, dict)
    assert _version(with_inputs["verifier-version"]) >= _VERIFIER_FLOOR, (
        "verifier-version must be at or above the reusable's VERIFIER_FLOOR 0.4.303"
    )


@pytest.mark.parametrize(
    ("version", "expected"),
    [("0.4.280", False), ("0.4.302", False), ("0.4.303", True), ("0.4.305", True)],
)
def test_floor_comparison_has_a_refusing_and_an_admitting_case(
    version: str, expected: bool
) -> None:
    assert (_version(version) >= _VERIFIER_FLOOR) is expected
