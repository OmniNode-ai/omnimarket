# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every named author's pull request lands like the operator's; no author lands for nobody (OMN-22192)."""

import pytest

from omnimarket.nodes.node_landing_policy_compute.handlers.handler_landing_policy import (
    POLICY_LINE,
    HandlerLandingPolicy,
)
from omnimarket.nodes.node_landing_policy_compute.models import (
    ModelLandingPolicyRequest,
)


@pytest.mark.parametrize(
    ("login", "operator", "is_bot", "lands", "controller"),
    [
        ("operator-login", "operator-login", False, True, True),
        ("a-collaborator", "operator-login", False, True, True),
        ("dependabot", "operator-login", True, True, True),
        ("", "operator-login", False, False, False),
        ("", "", True, False, True),
    ],
)
def test_policy(
    login: str, operator: str, is_bot: bool, lands: bool, controller: bool
) -> None:
    verdict = HandlerLandingPolicy().handle(
        ModelLandingPolicyRequest(login=login, operator=operator, is_bot=is_bot)
    )
    assert verdict.lands_like_operator is lands
    assert verdict.controller_lands is controller
    assert verdict.policy == POLICY_LINE
