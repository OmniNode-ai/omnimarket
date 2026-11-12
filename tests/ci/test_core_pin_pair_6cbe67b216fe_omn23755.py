# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23839: omnimarket takes omnibase_infra a349fbf4478e beside omnibase_core 6cbe67b216fe.

omnimarket takes omnibase_infra a349fbf4478e (omnibase_infra#5894, which moves
infra to core 6cbe67b216fe) beside omnibase_core 6cbe67b216fe, which carries
the delegation routing order models (omnibase_core#2309). A core-only move
left infra 25ae57b628a4 pinning core cd128b790ec6 and the Sibling Release
Compatibility Gate refused the head with conflicting URLs for omnibase-core.
"""

from __future__ import annotations

import importlib

import pytest

from tests.ci.test_sibling_core_pin_pair_omn17427 import (
    INFRA_CORE_TAGS,
    core_tag_conflict,
)

pytestmark = pytest.mark.unit

CORE_TAG = "dev-20261111160725-6cbe67b216fe"
CORE_SHA = "6cbe67b216fe53d7db97d012d26cd5aa04ff9404"
INFRA_TAG = "dev-20261111190933-a349fbf4478e"
INFRA_SHA = "a349fbf4478e4c6705115aab7175089b0b857f4c"
OLD_CORE_TAG = "dev-20261109065232-cd128b790ec6"
OLD_INFRA_SHA = "25ae57b628a4824116f0b04eea03097a092ddbc5"


def test_the_6cbe67b216fe_pair_is_recorded_and_conflict_free() -> None:
    """Infra a349fbf4478e (omnibase_infra#5894) and core 6cbe67b216fe land as one pair."""
    assert (INFRA_SHA[:12], CORE_TAG) in INFRA_CORE_TAGS
    assert core_tag_conflict(INFRA_SHA, CORE_TAG) is None
    assert INFRA_TAG.endswith(INFRA_SHA[:12])
    assert CORE_TAG.endswith(CORE_SHA[:12])


def test_a_core_only_bump_is_still_named_as_the_conflict() -> None:
    """Negative control: core 6cbe67b216fe beside infra 25ae57b628a4 is refused."""
    assert core_tag_conflict(OLD_INFRA_SHA, CORE_TAG) == (
        f"omnibase_infra {OLD_INFRA_SHA[:12]} pins omnibase-core "
        f"{OLD_CORE_TAG}, omnimarket pins {CORE_TAG}; move both pins in one change"
    )


def test_the_old_core_beside_the_new_infra_is_named_as_the_conflict() -> None:
    """Negative control: omnimarket on cd128b790ec6 beside infra a349fbf4478e is refused."""
    assert core_tag_conflict(INFRA_SHA, OLD_CORE_TAG) == (
        f"omnibase_infra {INFRA_SHA[:12]} pins omnibase-core "
        f"{CORE_TAG}, omnimarket pins {OLD_CORE_TAG}; move both pins in one change"
    )


def test_the_pinned_core_carries_the_routing_order_models() -> None:
    """Core 6cbe67b216fe (omnibase_core#2309) ships the delegation routing order models."""
    for module_name, class_name in (
        (
            "model_delegation_routing_order_request",
            "ModelDelegationRoutingOrderRequest",
        ),
        (
            "model_delegation_routing_order_rung",
            "ModelDelegationRoutingOrderRung",
        ),
        (
            "model_delegation_routing_order_answer",
            "ModelDelegationRoutingOrderAnswer",
        ),
    ):
        module = importlib.import_module(
            f"omnibase_core.models.delegation.{module_name}"
        )
        assert hasattr(module, class_name)
