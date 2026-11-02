# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed I/O for one night of the dev-tag pin train (OMN-21895)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_dev_tag_pin_mover_compute.models.model_dev_tag_pin_set import (
    ModelPinSetResult,
)


class EnumTrainStatus(StrEnum):
    """How a night ended. Only ``published`` pushes a branch or opens a PR."""

    PUBLISHED = "published"
    DRY_RUN = "dry_run"
    UNCHANGED = "unchanged"
    SKIPPED_NO_CO_RESOLVING_SET = "skipped_no_co_resolving_set"
    SKIPPED_RESOLVE_FAILED = "skipped_resolve_failed"


class ModelResolveOutcome(BaseModel):
    """``uv lock`` on the candidate pins in a scratch copy of the consumer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    lock_text: str = ""
    stderr: str = ""
    conflicting_urls: tuple[str, ...] = ()


class ModelTrainReport(BaseModel):
    """The night's verdict, printed as the plan and read by the workflow."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumTrainStatus
    selection: ModelPinSetResult
    body: str = ""
    resolve: ModelResolveOutcome | None = None
    publication: str = ""
    skipped_row: str = ""
