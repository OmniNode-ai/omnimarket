# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""All metrics, ordered findings and warnings for one draft."""

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_standup_draft_check_compute.models.model_standup_finding import (
    ModelStandupFinding,
)


class ModelStandupDraftCheckResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ok: bool
    words: int
    themes: int
    links: int
    refs: int
    findings: tuple[ModelStandupFinding, ...]
    warnings: tuple[str, ...] = ()
