# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where the fill reads merged work from beside the PR watcher: the canonical clones' default branches (OMN-23376).

The settings are the ``merged_history`` block of this node's contract.yaml; no value is written in code. The watcher holds only
the merges of its own window, so a ticket merged earlier than that read as unbuilt and was dispatched again every half hour.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib import resources

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    find_node_overlay,
)

NODE_NAME = "node_lab_fill_effect"


class ModelLabFillMergedHistory(BaseModel):
    """The clones, the refs and the window the merged-history read covers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Clone directory names, looked for under the registry root and beside it.
    repos: tuple[str, ...] = Field(min_length=1)
    # The first of these refs a clone has is its default branch.
    base_refs: tuple[str, ...] = Field(min_length=1)
    since_days: int = Field(ge=1)
    # A commit whose subject starts this way is change-control evidence, never implementation.
    evidence_prefix: str = Field(min_length=1)


def load_merged_history(
    environ: Mapping[str, str] | None = None,
) -> ModelLabFillMergedHistory:
    """The ``merged_history`` block of the node's contract.yaml, plus the deployment overlay's ``extra_repos`` (the private
    clones; the overlay's absence reads as none). A missing block, or an overlay that is not a mapping of a list of names,
    refuses by name."""
    text = (
        resources.files("omnimarket.nodes.node_lab_fill_effect")
        .joinpath("contract.yaml")
        .read_text()
    )
    block = (yaml.safe_load(text) or {}).get("merged_history")
    if not isinstance(block, dict):
        raise ValueError(
            "node_lab_fill_effect contract.yaml has no merged_history block"
        )
    settings = ModelLabFillMergedHistory.model_validate(block)
    overlay = find_node_overlay(NODE_NAME, environ)
    if overlay is None:
        return settings
    document = yaml.safe_load(overlay.read_text(encoding="utf-8")) or {}
    extra = document.get("extra_repos", []) if isinstance(document, dict) else None
    if not isinstance(extra, list) or not all(isinstance(n, str) and n for n in extra):
        raise NodeOverlayError(f"{overlay}: extra_repos must be a list of clone names")
    return settings.model_copy(
        update={
            "repos": (*settings.repos, *(n for n in extra if n not in settings.repos))
        }
    )
