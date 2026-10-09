# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The contract a DoD verdict evaluated, resolved at collection (OMN-20696)."""

from __future__ import annotations

import re
from typing import Self

from pydantic import BaseModel, ConfigDict, model_validator

from omnimarket.enums.enum_dod_contract_source import EnumDodContractSource


class ModelDodContractSubject(BaseModel):
    """A bound contract's repository, full object id and relative file path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: EnumDodContractSource
    repository: str | None
    commit_sha: str | None
    repo_path: str | None

    @model_validator(mode="after")
    def _validate_binding(self) -> Self:
        if (
            self.commit_sha is not None
            and re.fullmatch(r"([0-9a-f]{40}|[0-9a-f]{64})", self.commit_sha) is None
        ):
            raise ValueError("commit_sha must be a full lowercase hex object id")
        if self.source in (
            EnumDodContractSource.PRODUCT_REPOSITORY,
            EnumDodContractSource.ONEX_CHANGE_CONTROL,
        ):
            if any(
                value is None
                for value in (self.repository, self.commit_sha, self.repo_path)
            ):
                raise ValueError(
                    "a bound source requires repository, commit_sha and repo_path"
                )
        elif self.commit_sha is not None:
            raise ValueError("an unbound or inline contract cannot claim a commit")
        if (
            self.source is EnumDodContractSource.INLINE_GOAL
            and self.repository is not None
        ):
            raise ValueError("an inline goal cannot claim a repository")
        return self


__all__ = ["ModelDodContractSubject"]
