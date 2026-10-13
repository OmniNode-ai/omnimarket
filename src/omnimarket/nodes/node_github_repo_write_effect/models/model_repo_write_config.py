# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The write effect's mode, read from its contract's ``write_config`` block.

The landing orchestrator's ``landing_config`` (OMN-20866) is the precedent: a
default ``github_mode`` and a per-repository override. A per-owner row sits
between them, so the registry repositories write for real without naming each
one, and a repository of any other owner keeps the default. A repository row
wins over its owner's row. An unknown key or mode is refused at load, and so is
an owner row that names a repository.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_github_repo_write_effect.models.model_repo_write_io import (
    EnumRepoWriteMode,
)


class ModelRepoWriteConfig(BaseModel):
    """Which repositories the effect writes to for real."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    github_mode: EnumRepoWriteMode = EnumRepoWriteMode.DRY_RUN
    github_mode_by_owner: dict[str, EnumRepoWriteMode] = Field(default_factory=dict)
    github_mode_by_repository: dict[str, EnumRepoWriteMode] = Field(
        default_factory=dict
    )

    @field_validator("github_mode_by_owner")
    @classmethod
    def _owner_rows_name_owners(
        cls, rows: dict[str, EnumRepoWriteMode]
    ) -> dict[str, EnumRepoWriteMode]:
        for owner in rows:
            if not owner or "/" in owner:
                raise ValueError(
                    f"github_mode_by_owner row {owner!r} must name an owner, "
                    "not a repository; a repository goes in github_mode_by_repository"
                )
        return rows

    @classmethod
    def from_contract(cls, contract_path: Path) -> ModelRepoWriteConfig:
        raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("write_config"), dict):
            raise ValueError(f"{contract_path} declares no write_config block")
        return cls.model_validate(raw["write_config"])

    def mode_for(self, repo: str) -> EnumRepoWriteMode:
        """The mode of ``repo``: its repository row, else its owner's row, else
        the default. Names compare case-insensitively."""
        wanted = repo.lower()
        for name, mode in self.github_mode_by_repository.items():
            if name.lower() == wanted:
                return mode
        owner = wanted.partition("/")[0]
        for name, mode in self.github_mode_by_owner.items():
            if name.lower() == owner:
                return mode
        return self.github_mode


__all__: list[str] = ["ModelRepoWriteConfig"]
