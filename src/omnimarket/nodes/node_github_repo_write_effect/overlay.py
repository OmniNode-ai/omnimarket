# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_github_repo_write_effect: the labels a pr_label write may touch, none in code.

Found at ``<root>/node_github_repo_write_effect/overlay.yaml`` under a root named in ``ONEX_SKILL_OVERLAY_ROOTS``. The
package ships ``overlay.example.yaml`` with synthetic values. Nothing here has a default: an absent key refuses the load.

The document also carries ``event_bus`` and ``comment_author_logins`` for their own readers; they are optional here
because the node never reads them, and each reader refuses on its own when its key is absent.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.bus_binding.model_node_event_bus import ModelNodeEventBus

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
    event_bus: ModelNodeEventBus | None = None
    comment_author_logins: tuple[str, ...] | None = None

    @field_validator("comment_author_logins")
    @classmethod
    def _comment_author_logins(
        cls, value: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        if value is None:
            return value
        if not value:
            raise ValueError("comment_author_logins is empty")
        seen: set[str] = set()
        for login in value:
            if not login.strip():
                raise ValueError("a comment author login is blank")
            if login in seen:
                raise ValueError("a comment author login is named twice")
            seen.add(login)
        return value


def load_overlay(environ: Mapping[str, str] | None = None) -> ModelRepoWriteOverlay:
    return load_structured_overlay(NODE_NAME, ModelRepoWriteOverlay, environ)
