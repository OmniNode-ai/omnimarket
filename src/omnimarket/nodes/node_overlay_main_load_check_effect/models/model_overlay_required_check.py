# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the overlay required check (OMN-26490): which nodes to judge, what each requires, and what was found."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelOverlayRequirement(BaseModel):
    """The overlay a node reads: its id, whether the node refuses to run without it, and how that was detected."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str
    overlay_id: str
    required: bool
    source: Literal["contract_block", "example_file"]


class ModelOverlayRequiredCommand(BaseModel):
    """The nodes to judge against the overlays tree, and the pull request body that may name a merged overlays pull request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo_root: str = Field(min_length=1)
    overlays_root: str = Field(min_length=1)
    nodes: tuple[str, ...]
    pr_body: str = ""
    overlays_checkout: str | None = None
    # A host's snapshot of the settings store (<root>/<overlay id>/overlay.yaml with a stamp); unset when the runner holds none.
    store_snapshot_root: str | None = None
    # OMN-24052: this pull request's head sha; set, an open overlay-repo pull request that the body names and that pins the node at this
    # sha may satisfy the requirement (the paired route). Unset, only a merged overlay-repo pull request does.
    pair_head_sha: str | None = None


class ModelOverlayRequiredFinding(BaseModel):
    """One node that requires an overlay the overlays tree does not hold."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node: str
    overlay_id: str
    looked_for: str
    detail: str


class ModelOverlayRequiredResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    judged: tuple[str, ...]
    findings: tuple[ModelOverlayRequiredFinding, ...]
    # OMN-24052: one line per node satisfied through an open paired overlay-repo pull request.
    paired: tuple[str, ...] = ()
