# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Required coverage for an owner-summary draft."""

from pydantic import BaseModel, ConfigDict


class ModelDraftCoverage(BaseModel):
    """Counts and citations a draft must include."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: str = ""
    headline: str = ""
    changes: tuple[str, ...] = ()
    merged_tickets: tuple[str, ...] = ()
    untracked_changes: tuple[str, ...] = ()
    dependency_changes: int = 0
    tickets: tuple[str, ...] = ()
    ticket_kinds: dict[str, str] = {}
    counts: dict[str, int] = {}
