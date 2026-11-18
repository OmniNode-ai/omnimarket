# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed overlay of the release sibling-premise read effect."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_release_sibling_premise_effect"


class ModelSiblingPremiseRepo(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    clone_dir: str
    base_branch: str


class ModelSiblingPremiseReadOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repos: tuple[ModelSiblingPremiseRepo, ...]
    classifier_path: str
    git_timeout_seconds: int = Field(ge=1)
    classifier_timeout_seconds: int = Field(ge=1)
    pending_window_seconds: int = Field(ge=1)
    lookback_hours: int = Field(ge=1)
    max_walk_commits: int = Field(ge=1)

    def repo(self, name: str) -> ModelSiblingPremiseRepo | None:
        for r in self.repos:
            if r.repo == name:
                return r
        return None


def load_overlay() -> ModelSiblingPremiseReadOverlay:
    return load_structured_overlay(NODE_NAME, ModelSiblingPremiseReadOverlay)
