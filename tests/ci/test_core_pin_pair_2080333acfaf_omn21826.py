# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-21826: omnimarket takes omnibase_core 2080333acfaf beside the omnibase_infra that pins it.

node_lab_receipt_check_effect needs the omnibase_core lab_receipt validator.
A core-only move to the validator tag left omnibase_infra on 3f93a2cc6f6c,
whose own pyproject names core 5bb91ae7e720, and the Sibling Release
Compatibility Gate refused the head with conflicting URLs for omnibase-core.
omnibase_infra 858c2c689461 pins core 2080333acfaf, which carries the
validator, so this change moves both omnimarket pins to that pair together.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from tests.ci.test_sibling_core_pin_pair_omn17427 import (
    INFRA_CORE_TAGS,
    core_tag_conflict,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

CORE_TAG = "dev-20261101203942-2080333acfaf"
CORE_SHA = "2080333acfaf6c076313be3ceadea9bc0394aaee"
INFRA_TAG = "dev-20261102125940-858c2c689461"
INFRA_SHA = "858c2c689461d022a0823efb6340e4ffd5e82f8a"


def _locked_source(name: str) -> str:
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    entries = [pkg for pkg in lock["package"] if pkg["name"] == name]
    assert len(entries) == 1, f"{name}: expected one uv.lock entry, got {len(entries)}"
    return str(entries[0]["source"].get("git", ""))


def test_the_lock_pins_core_2080333acfaf_and_the_infra_that_names_it() -> None:
    assert _locked_source("omnibase-core").endswith(f"?tag={CORE_TAG}#{CORE_SHA}")
    assert _locked_source("omnibase-infra").endswith(f"?tag={INFRA_TAG}#{INFRA_SHA}")


def test_the_new_pair_is_recorded_as_the_newest_row() -> None:
    assert INFRA_CORE_TAGS[-1] == (INFRA_SHA[:12], CORE_TAG)
    assert core_tag_conflict(INFRA_SHA, CORE_TAG) is None


def test_the_previous_infra_beside_the_new_core_is_still_named_as_the_conflict() -> (
    None
):
    """Negative control: core 2080333acfaf beside infra 3f93a2cc6f6c is refused."""
    assert core_tag_conflict("3f93a2cc6f6c18c7ad4fa6e74738908692c21449", CORE_TAG) == (
        "omnibase_infra 3f93a2cc6f6c pins omnibase-core "
        "dev-20261031202655-5bb91ae7e720, omnimarket pins "
        f"{CORE_TAG}; move both pins in one change"
    )
