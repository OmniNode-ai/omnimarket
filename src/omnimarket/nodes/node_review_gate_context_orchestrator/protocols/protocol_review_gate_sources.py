# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The source ports of the review-gate context sweep."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Protocol

from omnimarket.models.model_landing_review import ModelLandingReviewFacts
from omnimarket.models.model_review_context_post import (
    ModelReviewContextPostCommand,
    ModelReviewContextPostResult,
)
from omnimarket.models.model_review_gate_context import ModelReviewGatePrSubject


class ReviewGateSourceError(RuntimeError):
    """A source could not be read; the sweep reads nothing and posts nothing."""


class ProtocolOpenPullRequestSource(Protocol):
    """The open pull requests of the requested repositories."""

    def open_pull_requests(
        self, repositories: tuple[str, ...]
    ) -> tuple[ModelReviewGatePrSubject, ...]:
        """Each subject carries review=None; the verdict is attached by the orchestrator."""
        ...


class ProtocolReviewVerdictSource(Protocol):
    """The recorded review verdicts, keyed by the bare repository name and number."""

    def review_facts(self, now: datetime) -> Mapping[str, ModelLandingReviewFacts]:
        """The review facts of each pull request a recorded verdict speaks of."""
        ...


class ProtocolContextPoster(Protocol):
    """Posts one head's review-gate context commit status."""

    def handle(
        self, command: ModelReviewContextPostCommand
    ) -> ModelReviewContextPostResult:
        """Posts the decision's commit status on the exact head, idempotently."""
        ...
