# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The write effect's mode, read from its contract's ``write_config`` block.

The landing orchestrator's ``landing_config`` (OMN-20866) is the precedent: a
default ``github_mode`` and a per-repository override. A repository not named
takes the default. An unknown key or mode is refused at load.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_github_repo_write_effect.models.model_repo_write_io import (
    EnumRepoWriteMode,
)


class ModelRepoWriteConfig(BaseModel):
    """Which repositories the effect writes to for real."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    github_mode: EnumRepoWriteMode = EnumRepoWriteMode.DRY_RUN
    github_mode_by_repository: dict[str, EnumRepoWriteMode] = Field(
        default_factory=dict
    )

    @classmethod
    def from_contract(cls, contract_path: Path) -> ModelRepoWriteConfig:
        raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("write_config"), dict):
            raise ValueError(f"{contract_path} declares no write_config block")
        return cls.model_validate(raw["write_config"])

    def mode_for(self, repo: str) -> EnumRepoWriteMode:
        """The declared mode of ``repo`` (case-insensitive), else the default."""
        wanted = repo.lower()
        for name, mode in self.github_mode_by_repository.items():
            if name.lower() == wanted:
                return mode
        return self.github_mode


__all__: list[str] = ["ModelRepoWriteConfig"]
