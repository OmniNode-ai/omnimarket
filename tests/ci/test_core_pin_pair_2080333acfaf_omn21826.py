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

import pytest

from tests.ci.test_sibling_core_pin_pair_omn17427 import (
    INFRA_CORE_TAGS,
    core_tag_conflict,
)

pytestmark = pytest.mark.unit

CORE_TAG = "dev-20261101203942-2080333acfaf"
CORE_SHA = "2080333acfaf6c076313be3ceadea9bc0394aaee"
INFRA_TAG = "dev-20261102125940-858c2c689461"
INFRA_SHA = "858c2c689461d022a0823efb6340e4ffd5e82f8a"


def test_the_2080333acfaf_pair_stays_recorded_and_conflict_free() -> None:
    """A later infra-only move (infra#5602, beb08a83c93e) superseded this infra as the lock."""
    assert (INFRA_SHA[:12], CORE_TAG) in INFRA_CORE_TAGS
    assert core_tag_conflict(INFRA_SHA, CORE_TAG) is None
    assert INFRA_TAG.endswith(INFRA_SHA[:12])
    assert CORE_TAG.endswith(CORE_SHA[:12])


def test_the_previous_infra_beside_the_new_core_is_still_named_as_the_conflict() -> (
    None
):
    """Negative control: core 2080333acfaf beside infra 3f93a2cc6f6c is refused."""
    assert core_tag_conflict("3f93a2cc6f6c18c7ad4fa6e74738908692c21449", CORE_TAG) == (
        "omnibase_infra 3f93a2cc6f6c pins omnibase-core "
        "dev-20261031202655-5bb91ae7e720, omnimarket pins "
        f"{CORE_TAG}; move both pins in one change"
    )
