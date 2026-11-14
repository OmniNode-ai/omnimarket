# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one Linear read boundary: changed issues, and who revised a description."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models import (
    ModelDescriptionRevision,
    ModelLinearIssueSnapshot,
)


class ProtocolLinearIssueSource(Protocol):
    """Both reads raise when Linear cannot be read; the handler turns that into detector_unreadable."""

    def list_issues_updated_after(
        self, updated_after: datetime
    ) -> list[ModelLinearIssueSnapshot]: ...

    def description_revisions(
        self, issue: ModelLinearIssueSnapshot
    ) -> list[ModelDescriptionRevision]: ...
