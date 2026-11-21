# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Structural boundary for the two Linear reads."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models import (
    ModelDescriptionRevision,
    ModelLinearIssueSnapshot,
)


class ProtocolLinearIssueReader(Protocol):
    """Implemented by the Linear GraphQL gateway and hermetic test readers."""

    def issues_updated_after(
        self, cursor: datetime, *, max_pages: int
    ) -> Sequence[ModelLinearIssueSnapshot]:
        """Every issue whose updatedAt is after the cursor; raises when Linear cannot be read."""
        ...

    def description_revisions(
        self, document_content_id: str
    ) -> Sequence[ModelDescriptionRevision]:
        """Every revision of one description document; raises when it cannot be read."""
        ...
