# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lane write carries a declared non-person actor and is refused under the operator (OMN-20355).

The actor id and the operator's principals come from configuration (the
environment), never from this repository. With no actor configured the check
stays silent, so a host that has not been given an actor writes as before.
"""

from __future__ import annotations

import pytest

from omnimarket.handlers.handler_lane_write_identity import (
    ENV_AUTOMATION_ACTOR_ID,
    ENV_OPERATOR_PRINCIPALS,
    LaneWriteIdentityRefusedError,
    ModelLaneWriteIdentity,
    assert_github_token_is_actor,
    assert_linear_principal_is_actor,
)

pytestmark = pytest.mark.unit

_ACTOR = "11111111-2222-4333-8444-555555555555"
_OPERATOR = "99999999-8888-4777-8666-555555555555"


def _identity() -> ModelLaneWriteIdentity:
    return ModelLaneWriteIdentity(
        automation_actor_id=_ACTOR, operator_principals=frozenset({_OPERATOR})
    )


def test_unconfigured_environment_declares_no_identity() -> None:
    assert ModelLaneWriteIdentity.from_env({}) is None
    assert ModelLaneWriteIdentity.from_env({ENV_AUTOMATION_ACTOR_ID: "  "}) is None


def test_environment_declares_actor_and_operator_principals() -> None:
    identity = ModelLaneWriteIdentity.from_env(
        {
            ENV_AUTOMATION_ACTOR_ID: _ACTOR,
            ENV_OPERATOR_PRINCIPALS: f" {_OPERATOR} , Some-Login ,",
        }
    )
    assert identity is not None
    assert identity.automation_actor_id == _ACTOR
    assert identity.operator_principals == frozenset({_OPERATOR, "some-login"})


def test_actor_id_that_is_an_email_address_is_refused() -> None:
    with pytest.raises(ValueError, match="person"):
        ModelLaneWriteIdentity.from_env(
            {ENV_AUTOMATION_ACTOR_ID: "someone@example.com"}
        )


def test_linear_write_under_the_operator_principal_is_refused() -> None:
    with pytest.raises(LaneWriteIdentityRefusedError, match="operator principal"):
        assert_linear_principal_is_actor(
            _identity(), surface="comment", principal=_OPERATOR
        )


def test_linear_write_under_a_principal_that_is_not_the_actor_is_refused() -> None:
    with pytest.raises(LaneWriteIdentityRefusedError, match="not the declared actor"):
        assert_linear_principal_is_actor(
            _identity(), surface="create", principal="other"
        )


def test_linear_write_with_no_resolvable_principal_is_refused() -> None:
    with pytest.raises(LaneWriteIdentityRefusedError, match="could not be resolved"):
        assert_linear_principal_is_actor(_identity(), surface="create", principal="")


def test_linear_write_under_the_actor_passes() -> None:
    assert_linear_principal_is_actor(_identity(), surface="create", principal=_ACTOR)


def test_unconfigured_identity_never_refuses() -> None:
    assert_linear_principal_is_actor(None, surface="create", principal=_OPERATOR)
    assert_github_token_is_actor(None, token="ghp_" + "a" * 36)


@pytest.mark.parametrize(
    "token", ["ghp_" + "a" * 36, "github_pat_" + "a" * 40, "gho_" + "a" * 36]
)
def test_github_person_token_is_refused_once_actor_is_configured(token: str) -> None:
    with pytest.raises(LaneWriteIdentityRefusedError, match="installation token"):
        assert_github_token_is_actor(_identity(), token=token)


def test_github_app_installation_token_passes() -> None:
    assert_github_token_is_actor(_identity(), token="ghs_" + "a" * 36)
