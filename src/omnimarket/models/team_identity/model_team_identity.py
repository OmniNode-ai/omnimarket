# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The team identity declaration, its resolved source, and the role a caller resolves to.

Shared here so a node that resolves a caller's role (plans-today, session recovery) types its
call to ``node_team_identity_compute`` without reaching into that node's private models package.

The declaration is ``<root>/team_identity/overlay.yaml`` under ``ONEX_SKILL_OVERLAY_ROOTS``. It
carries exactly one ``role: operator`` entry and any number of ``role: member`` entries. Each entry
names a Slack member id, a Linear user id, a GitHub login, its ledger principals and an optional
``reports_to`` naming another entry. The values belong to the operator; this package ships none.

Validation messages name entry positions and fields, never values, which are private deployment facts.
"""

from __future__ import annotations

from collections import Counter
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

TEAM_IDENTITY_SCHEMA_VERSION = 1

NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
TeamRole = Literal["operator", "member"]
# ``operator`` only for an authenticated match of the one operator entry; everything else is ``team``.
TeamPrivileges = Literal["operator", "team"]
# Where a claimed identity came from. Only the authenticated sources can identify an entry; an
# ``environment`` claim (a ``principal=`` string, a variable) is never positive identification.
ClaimSource = Literal[
    "slack_auth", "linear_auth", "github_auth", "broker_binding", "environment"
]


class ModelTeamIdentityEntry(BaseModel):
    """One person on the team; every field but ``reports_to`` is required."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: NonEmpty
    role: TeamRole
    slack_member_id: NonEmpty
    linear_user_id: NonEmpty
    github_login: NonEmpty
    ledger_principals: Annotated[tuple[NonEmpty, ...], Field(min_length=1)]
    reports_to: NonEmpty | None = None


def _duplicates(values: list[str]) -> list[str]:
    return sorted(value for value, count in Counter(values).items() if count > 1)


class ModelTeamIdentityOverlay(BaseModel):
    """``<root>/team_identity/overlay.yaml``: the versioned declaration of who the operator is."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    entries: Annotated[tuple[ModelTeamIdentityEntry, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _one_operator_unique_ids_known_references(self) -> ModelTeamIdentityOverlay:
        operators = [
            index
            for index, entry in enumerate(self.entries)
            if entry.role == "operator"
        ]
        if len(operators) != 1:
            raise ValueError(
                f"exactly one entry must carry role operator; entries {operators} do"
            )
        for field in ("id", "slack_member_id", "linear_user_id", "github_login"):
            values = [getattr(entry, field) for entry in self.entries]
            positions = [
                index
                for index, value in enumerate(values)
                if value in _duplicates(values)
            ]
            if positions:
                raise ValueError(f"{field} repeats across entries {positions}")
        principals = [
            principal for entry in self.entries for principal in entry.ledger_principals
        ]
        repeated = _duplicates(principals)
        if repeated:
            positions = [
                index
                for index, entry in enumerate(self.entries)
                if set(entry.ledger_principals) & set(repeated)
            ]
            raise ValueError(f"ledger_principals repeat across entries {positions}")
        ids = {entry.id for entry in self.entries}
        for index, entry in enumerate(self.entries):
            if entry.reports_to is None:
                continue
            if entry.reports_to == entry.id:
                raise ValueError(f"entries.{index}.reports_to names its own entry")
            if entry.reports_to not in ids:
                raise ValueError(f"entries.{index}.reports_to names no entry")
        return self

    @property
    def operator(self) -> ModelTeamIdentityEntry:
        return next(entry for entry in self.entries if entry.role == "operator")


class ModelTeamIdentitySource(BaseModel):
    """Where the declaration in force came from, for callers' proof receipts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    sha256: str
    schema_version: int


class ModelResolvedTeamIdentity(BaseModel):
    """A validated declaration together with its resolved source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay: ModelTeamIdentityOverlay
    source: ModelTeamIdentitySource


class ModelIdentityClaim(BaseModel):
    """One identity a caller observed, and where it came from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: ClaimSource
    value: NonEmpty


class ModelTeamIdentityRequest(BaseModel):
    """Resolve the caller's role from its claims against the declaration under ``overlay_roots``.

    ``overlay_roots`` ``None`` reads ``ONEX_SKILL_OVERLAY_ROOTS`` from the process environment.
    ``developer_principals`` (the developer principal overlay's rows) and ``lab_host_logins`` (the
    lab-host logins of the dev-lane ACLs), when supplied, are cross-checked against the declaration.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    claims: tuple[ModelIdentityClaim, ...]
    overlay_roots: tuple[str, ...] | None = None
    developer_principals: frozenset[str] | None = None
    lab_host_logins: frozenset[str] | None = None


class ModelTeamIdentityResult(BaseModel):
    """The resolved role. ``privileges`` is ``team`` unless an authenticated claim matched the operator.

    ``diagnostic`` names why identification fell short (unknown, unavailable, mismatched or a
    malformed declaration); it is ``None`` only for a clean match.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    privileges: TeamPrivileges
    entry_id: str | None
    role: TeamRole | None
    diagnostic: str | None
    source: ModelTeamIdentitySource | None
