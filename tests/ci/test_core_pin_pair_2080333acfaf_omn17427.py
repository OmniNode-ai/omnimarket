# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: omnimarket takes omnibase_core 2080333acfaf beside the omnibase_infra that pins it.

The omnibase_core dependency-pin bot opened core-only bump PRs here
(omnimarket#4652 to #4740), and every one was refused by the Sibling Release
Compatibility Gate (OMN-20381) because the pinned omnibase_infra commit still
named an older core tag. omnibase_core#2208 withdrew that bot.
omnibase_infra dev (c352b5618a45) moved its own core pin to 2080333acfaf, and this
change moves both omnimarket pins to that pair in one commit, as
tests/ci/test_sibling_core_pin_pair_omn17427.py requires.

At the merge base the lock pins infra 3f93a2cc6f6c and core 5bb91ae7e720, so
every test here fails there.
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
INFRA_TAG = "dev-20261102124307-c352b5618a45"
INFRA_SHA = "c352b5618a45c31f686e94750098edb0d5913e28"


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
