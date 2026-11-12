# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_github_repo_write_effect: the labels a pr_label write may touch, none in code.

Found at ``<root>/node_github_repo_write_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The
package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the load.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

NODE_NAME = "node_github_repo_write_effect"


class ModelPrLabelOverlay(BaseModel):
    """The labels a pr_label write may add or remove; a label outside this set is refused before any GitHub call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    allowed_labels: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelPrLabelOverlay:
        if len(self.allowed_labels) != len(set(self.allowed_labels)):
            raise ValueError("an allowed label is named twice")
        if any(not label.strip() for label in self.allowed_labels):
            raise ValueError("an allowed label is empty")
        return self


class ModelRepoWriteOverlay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_label: ModelPrLabelOverlay


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelRepoWriteOverlay:
    return load_structured_overlay(NODE_NAME, ModelRepoWriteOverlay, environ)
