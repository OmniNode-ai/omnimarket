# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The default landing wiring refuses a new-work head that has no fable-review-gate status.

The policy a landing tick carries when nothing sets ``review_status_read_expected`` is the default wiring. A new-work head
whose review fact holds no statuses (none were read) must not merge under it: it is held as ``review_status_unread``.
Every refusal has its control: the same head with a success status on it merges, and a PR that is not new work is decided as
before.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingLandSkipReason,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingPolicy,
)
from tests.unit.nodes.node_pr_landing_decision_compute.test_landing_review_status import (
    HEAD,
    _facts,
    _merges,
    _skip,
    _status,
)

pytestmark = pytest.mark.unit


def test_review_status_default_wiring_policy_expects_the_statuses_to_be_read() -> None:
    assert ModelLandingPolicy().review_status_read_expected is True


def test_review_status_default_wiring_refuses_a_new_work_head_with_no_status() -> None:
    facts = _facts(None)
    assert _merges(facts) == []
    assert _skip(facts) is EnumLandingLandSkipReason.REVIEW_STATUS_UNREAD


def test_review_status_default_wiring_merges_the_head_once_its_status_is_read() -> None:
    facts = _facts([_status(HEAD, "success")])
    assert _merges(facts) == [HEAD]
    assert _skip(facts) is None


def test_review_status_default_wiring_leaves_a_head_that_is_not_new_work_alone() -> (
    None
):
    assert _merges(_facts(None, new_work=False)) == [HEAD]
