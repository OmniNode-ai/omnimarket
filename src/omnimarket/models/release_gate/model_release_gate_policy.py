# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``release_gate`` overlay as the nodes see it (OMN-21492).

Shape only. Every value (the grant label, the role lists, the agent identities, the
train and tagger identities, the shapes that make an action a release) is the
deployment owner's, held in the private ``release_gate`` overlay. A login never
appears in this package, a test or a shared file. The caller loads the overlay once
and hands the policy to the nodes, which read nothing themselves.
"""

from __future__ import annotations

import hashlib
import re
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelReleaseActorShape(BaseModel):
    """How a named release actor (the train, the tagger) shows itself.

    The run identity is the identity the run carries (the application or token that
    ran it). A login or a branch name is only a string an agent can imitate, so an
    actor is the train only when its run identity is listed here as well.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    logins: tuple[str, ...] = ()
    run_identities: tuple[str, ...] = ()
    branch_prefixes: tuple[str, ...] = ()
    tag_patterns: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _patterns_compile(self) -> Self:
        for pattern in self.tag_patterns:
            re.compile(pattern)
        return self


class ModelReleaseShapes(BaseModel):
    """Which titles, labels, branches, files and tags make an action a release."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_title_prefixes: tuple[str, ...] = ()
    pr_labels: tuple[str, ...] = ()
    pr_branch_prefixes: tuple[str, ...] = ()
    version_files: tuple[str, ...] = ()
    version_tag_patterns: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _patterns_compile(self) -> Self:
        for pattern in self.version_tag_patterns:
            re.compile(pattern)
        return self


class ModelReleaseGatePolicy(BaseModel):
    """The ``release_gate`` overlay: ``<root>/release_gate/overlay.yaml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    grant_label: str = Field(min_length=1)
    grantor_roles: tuple[str, ...] = Field(min_length=1)
    role_logins: dict[str, tuple[str, ...]]
    agent_identities: tuple[str, ...] = ()
    train: ModelReleaseActorShape
    tagger: ModelReleaseActorShape
    shapes: ModelReleaseShapes

    @model_validator(mode="after")
    def _every_grantor_role_has_a_list(self) -> Self:
        missing = [role for role in self.grantor_roles if role not in self.role_logins]
        if missing:
            raise ValueError(f"grantor roles without a login list: {missing}")
        return self

    def digest(self) -> str:
        """A stable digest of the whole policy, bound into an authorization receipt."""
        return hashlib.sha256(
            self.model_dump_json(round_trip=True).encode("utf-8")
        ).hexdigest()
