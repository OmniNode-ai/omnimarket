# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Which identity a lane write goes out under, and the refusal when it is a person's (OMN-20355).

A lane that writes to Linear or GitHub with the operator's personal credential
is indistinguishable from the operator: the hourly comment sweep discards the
comments as ``operator_self`` and the operator's own workload cannot be
measured. The fix is a declared non-person actor (an OAuth application user on
Linear, a GitHub App installation on GitHub) and a check that refuses a write
that would still go out under a person.

Nothing in this module names a person. The actor id
(``ONEX_AUTOMATION_ACTOR_ID``) and the principals to refuse
(``ONEX_OPERATOR_PRINCIPALS``, comma separated) are read from the environment of
the process that writes. With no actor configured the check is silent, so a
host that has not been given an actor keeps writing as it does today; the
person steps that register the actor are in OMN-17664.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.github_app_auth import (
    GitHubAppIdentityError,
    assert_is_app_installation_token,
)

ENV_AUTOMATION_ACTOR_ID = "ONEX_AUTOMATION_ACTOR_ID"
ENV_OPERATOR_PRINCIPALS = "ONEX_OPERATOR_PRINCIPALS"


class LaneWriteIdentityRefusedError(RuntimeError):
    """A lane write would go out under a principal that is not the declared actor."""


class ModelLaneWriteIdentity(BaseModel):
    """The declared actor of lane writes and the principals a lane write must not use."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    automation_actor_id: str = Field(min_length=1)
    operator_principals: frozenset[str] = Field(default_factory=frozenset)

    @field_validator("automation_actor_id")
    @classmethod
    def _actor_is_not_a_person(cls, value: str) -> str:
        value = value.strip()
        if not value or "@" in value.replace("[bot]", ""):
            raise ValueError(
                f"{ENV_AUTOMATION_ACTOR_ID} must be a bot or OAuth actor id, "
                "not a person's email address"
            )
        return value

    @field_validator("operator_principals")
    @classmethod
    def _casefold(cls, value: frozenset[str]) -> frozenset[str]:
        return frozenset(item.strip().casefold() for item in value if item.strip())

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> ModelLaneWriteIdentity | None:
        """The identity the environment declares; ``None`` when no actor is set."""
        actor = environ.get(ENV_AUTOMATION_ACTOR_ID, "").strip()
        if not actor:
            return None
        principals = frozenset(environ.get(ENV_OPERATOR_PRINCIPALS, "").split(","))
        return cls(automation_actor_id=actor, operator_principals=principals)


def assert_linear_principal_is_actor(
    identity: ModelLaneWriteIdentity | None, *, surface: str, principal: str
) -> None:
    """Refuse a Linear write whose credential resolves to anyone but the actor.

    ``principal`` is the Linear viewer id the write credential resolves to.
    """
    if identity is None:
        return
    seen = principal.strip()
    if not seen:
        raise LaneWriteIdentityRefusedError(
            f"Linear {surface} refused: the principal of the write credential "
            "could not be resolved, so it cannot be shown to be the declared actor"
        )
    if seen.casefold() in identity.operator_principals:
        raise LaneWriteIdentityRefusedError(
            f"Linear {surface} refused: the credential resolves to an operator "
            f"principal ({ENV_OPERATOR_PRINCIPALS}); lane writes go out under "
            f"{ENV_AUTOMATION_ACTOR_ID} only"
        )
    if seen != identity.automation_actor_id:
        raise LaneWriteIdentityRefusedError(
            f"Linear {surface} refused: the credential resolves to a principal that "
            f"is not the declared actor ({ENV_AUTOMATION_ACTOR_ID})"
        )


def assert_github_token_is_actor(
    identity: ModelLaneWriteIdentity | None, *, token: str
) -> None:
    """Refuse a GitHub write whose token is not a GitHub App installation token.

    A person's classic or fine-grained token and an OAuth user token write as
    that person; only an installation token writes as the App's bot login.
    """
    if identity is None:
        return
    try:
        assert_is_app_installation_token(token)
    except GitHubAppIdentityError as exc:
        raise LaneWriteIdentityRefusedError(
            "GitHub write refused: the write token is not a GitHub App "
            f"installation token, so it would go out under a person ({ENV_AUTOMATION_ACTOR_ID} is set)"
        ) from exc


__all__: list[str] = [
    "ENV_AUTOMATION_ACTOR_ID",
    "ENV_OPERATOR_PRINCIPALS",
    "LaneWriteIdentityRefusedError",
    "ModelLaneWriteIdentity",
    "assert_github_token_is_actor",
    "assert_linear_principal_is_actor",
]
