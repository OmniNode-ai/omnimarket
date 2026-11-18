# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ports for reading and posting a head's review-gate commit status."""

from __future__ import annotations

from typing import Protocol

from omnimarket.models.model_review_context_post import (
    ModelHeadStatus,
)


class ReviewContextPortError(RuntimeError):
    """A port call failed; never carries a token."""


class ProtocolHeadStatusReader(Protocol):
    """Reads the head's commit statuses."""

    def read(
        self, *, repository: str, head_sha: str, context: str
    ) -> tuple[ModelHeadStatus, ...]:
        """The head's statuses of the context; a status of another head or context never counts."""
        ...


class ProtocolHeadStatusPoster(Protocol):
    """Posts a commit status on the head."""

    def post(
        self,
        *,
        correlation_id: str,
        repository: str,
        head_sha: str,
        context: str,
        state: str,
        target_url: str,
        description: str,
    ) -> int:
        """Posts the status and returns the new status id."""
        ...
