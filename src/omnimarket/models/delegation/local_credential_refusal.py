# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-18696: the typed credential refusal the bus-less local path returns.

There is no gateway on the local path, so none of the gateway refusals
(OMN-18042, OMN-17930, OMN-17940) is reachable from it. Those refusals fire in
the routing terminus, BEFORE a backend is selected, and they are about a
tenant's registered key. This one fires at the effect boundary, AFTER a backend
is selected, and it is about the provider credential that backend declares.

Two conditions, and telling them apart is the whole point:

``CREDENTIAL_ABSENT``
    The backend declares a credential reference and no value resolves for it.
    No provider call is attempted. The customer must register a value.

``CREDENTIAL_REJECTED``
    A value resolved, was presented, and the provider answered 401/403. The
    customer must replace the value.

Neither is retryable, and neither is a provider outage. Before this module the
local path returned ``MODEL_UNAVAILABLE`` for the rejected case -- the same
class an unreachable endpoint returns -- and ``UNKNOWN`` for the absent case,
because the resolver's exception fell through to a bare ``except Exception``.
Both are in the retryable set, so both escalated up the tier ladder: the
customer saw a slow climb through every tier followed by a generic failure,
which is the exact "retry loop, or a silent fallback to any other route"
this ticket's AC1 names as its falsifier.

Carries reference NAMES only, never a secret VALUE, on the same terms as
:mod:`omnimarket.tenant_credential_ref` -- every field here is safe to log,
publish and display by construction.

It lives under ``models.delegation`` and NOT under ``inference`` beside the
credential resolution it reports on, for one mechanical reason: this model is
reachable from ``ModelDelegateSkillResponse``, which the api-server import
graph pulls in, and ``omnimarket.inference.__init__`` eagerly imports the
inference bridge -- which reaches a database driver.
``test_api_server_import_never_loads_asyncpg_or_psycopg2_in_sys_modules``
catches that, and it caught this. A model that a wire response references
belongs in the model tree regardless; the import gate made the placement
non-negotiable rather than merely tidier.
"""

from __future__ import annotations

from enum import StrEnum, unique
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass


@unique
class EnumLocalCredentialRefusalReason(StrEnum):
    """Which credential condition refused this local delegation."""

    CREDENTIAL_ABSENT = "credential_absent"
    CREDENTIAL_REJECTED = "credential_rejected"


_FAILURE_CLASS_BY_REASON: Final[
    dict[EnumLocalCredentialRefusalReason, EnumDelegationFailureClass]
] = {
    EnumLocalCredentialRefusalReason.CREDENTIAL_ABSENT: (
        EnumDelegationFailureClass.PROVIDER_CREDENTIAL_MISSING
    ),
    EnumLocalCredentialRefusalReason.CREDENTIAL_REJECTED: (
        EnumDelegationFailureClass.PROVIDER_AUTH_FAILED
    ),
}

_REMEDIATION_BY_REASON: Final[dict[EnumLocalCredentialRefusalReason, str]] = {
    EnumLocalCredentialRefusalReason.CREDENTIAL_ABSENT: (
        "Register a value for this reference in the secret store, then retry."
    ),
    EnumLocalCredentialRefusalReason.CREDENTIAL_REJECTED: (
        "Replace this reference's value with one the provider accepts, then retry."
    ),
}


_DETAIL_SOURCE_BY_REASON: Final[dict[EnumLocalCredentialRefusalReason, str]] = {
    EnumLocalCredentialRefusalReason.CREDENTIAL_ABSENT: "The secret resolver said",
    EnumLocalCredentialRefusalReason.CREDENTIAL_REJECTED: "The provider said",
}


class ModelLocalCredentialRefusal(BaseModel):
    """A typed, non-retryable credential refusal from the local delegation path.

    ``retryable`` is a derived constant rather than a settable field: a caller
    that could set it could set it wrongly, and the escalation ladder reads it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: EnumLocalCredentialRefusalReason = Field(
        description="Which credential condition fired."
    )
    credential_ref: str | None = Field(
        default=None,
        description=(
            "The credential REFERENCE the backend declares, by name. ``None`` "
            "only when the backend declared none at all. Never a secret value."
        ),
    )
    credential_env: str | None = Field(
        default=None,
        description=(
            "The environment-variable NAME the backend declares as its fallback, "
            "when it declares one. Never a secret value."
        ),
    )
    backend_ref: str = Field(
        description="The endpoint reference whose credential was refused."
    )
    model_id: str = Field(description="The model the refused call would have run.")
    correlation_id: str = Field(
        description="Correlation id of the refused delegation, for the receipt."
    )
    detail: str = Field(
        default="",
        description=(
            "What the refusing party said, verbatim, when it said anything. The "
            "provider for a rejection, the secret resolver for an absence. "
            "Empty when neither said anything."
        ),
    )

    @property
    def failure_class(self) -> EnumDelegationFailureClass:
        """The escalation taxonomy's class for this reason."""
        return _FAILURE_CLASS_BY_REASON[self.reason]

    @property
    def remediation(self) -> str:
        """What the customer must do. Every reason maps; there is no default."""
        return _REMEDIATION_BY_REASON[self.reason]

    @property
    def retryable(self) -> bool:
        """Always ``False``. A credential fact does not change on re-ask."""
        return False

    @property
    def named_credential(self) -> str:
        """The credential this refusal is about, named for a human.

        Names the reference and the env-var fallback when both are declared,
        because a customer whose reference is right and whose fallback is wrong
        cannot act on either name alone.
        """
        names = [name for name in (self.credential_ref, self.credential_env) if name]
        if not names:
            return "the backend's provider credential (no reference declared)"
        return " / ".join(names)

    @property
    def message(self) -> str:
        """One human-readable line, safe to surface verbatim.

        The detail is attributed to whoever produced it. A resolver's "no value
        for this reference" rendered as "the provider said" would send a
        customer to check a provider that was never contacted.
        """
        said_by = _DETAIL_SOURCE_BY_REASON[self.reason]
        detail = f" {said_by}: {self.detail}" if self.detail else ""
        return (
            f"Delegation refused: {self.reason.value} for {self.named_credential} "
            f"on backend {self.backend_ref!r} (model {self.model_id!r}). "
            f"{self.remediation}{detail}"
        )


__all__ = [
    "EnumLocalCredentialRefusalReason",
    "ModelLocalCredentialRefusal",
]
