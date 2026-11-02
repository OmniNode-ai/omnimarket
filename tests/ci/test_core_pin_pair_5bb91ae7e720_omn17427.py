# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: omnimarket takes omnibase_core 5bb91ae7e720 beside the omnibase_infra that pins it.

The omnibase_core dependency-pin bot opened core-only bump PRs here
(omnimarket#4652 to #4740), and every one was refused by the Sibling Release
Compatibility Gate (OMN-20381) because the pinned omnibase_infra commit still
named core fc6159b98026. omnibase_core#2208 withdrew that bot.
omnibase_infra#5536 moved infra's own core pin to 5bb91ae7e720, and this
change moves both omnimarket pins to that pair in one commit, as
tests/ci/test_sibling_core_pin_pair_omn17427.py requires.

At the merge base the lock pins infra 4284f5bf5587 and core fc6159b98026, so
every test here fails there.
"""

from __future__ import annotations

import pytest

from tests.ci.test_sibling_core_pin_pair_omn17427 import (
    INFRA_CORE_TAGS,
    core_tag_conflict,
)

pytestmark = pytest.mark.unit

CORE_TAG = "dev-20261031202655-5bb91ae7e720"
CORE_SHA = "5bb91ae7e72094a62a1198dbc92803448a05ec67"
INFRA_TAG = "dev-20261101125736-3f93a2cc6f6c"
INFRA_SHA = "3f93a2cc6f6c18c7ad4fa6e74738908692c21449"


def test_the_5bb91ae7e720_pair_stays_recorded_and_conflict_free() -> None:
    """A later pin move (OMN-21826, core 2080333acfaf) superseded this pair as the lock."""
    assert (INFRA_SHA[:12], CORE_TAG) in INFRA_CORE_TAGS
    assert core_tag_conflict(INFRA_SHA, CORE_TAG) is None
    assert INFRA_TAG.endswith(INFRA_SHA[:12])
    assert CORE_TAG.endswith(CORE_SHA[:12])


def test_the_previous_infra_beside_the_new_core_is_still_named_as_the_conflict() -> (
    None
):
    """Negative control: core 5bb91ae7e720 beside infra 4284f5bf5587 is refused."""
    assert core_tag_conflict("4284f5bf55871b120353e650b7cce205882e252f", CORE_TAG) == (
        "omnibase_infra 4284f5bf5587 pins omnibase-core "
        "dev-20261031000524-fc6159b98026, omnimarket pins "
        f"{CORE_TAG}; move both pins in one change"
    )
