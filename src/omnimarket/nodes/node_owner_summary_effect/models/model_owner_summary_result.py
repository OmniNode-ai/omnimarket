# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The outcome of one owner-summary command."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_owner_summary_effect.models.model_owner_summary_facts import (
    ModelOwnerSummaryFacts,
)


class ModelOwnerSummaryResult(BaseModel):
    """Exit code, a one-line message, the facts (for facts) and the broken rules (for check)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    message: str
    facts: ModelOwnerSummaryFacts | None = None
    problems: tuple[str, ...] = ()
